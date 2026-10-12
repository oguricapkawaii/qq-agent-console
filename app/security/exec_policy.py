"""受控本地命令执行（exec 模式的唯一实现）。

安全边界（F01 整改后）：
- **平台隔离优先**：命令经 macOS `sandbox-exec` 运行，写只允许任务目录与任务私有临时
  目录、网络全禁、显式拒绝读取凭据与机器人自身目录（见 app.security.sandbox）。
  平台不支持可验证隔离时**明确禁用 exec**，绝不退回无隔离执行；
- 参数数组启动，绝不使用 shell（不存在 shell 注入面）；
- 可执行文件必须在配置允许清单内；提权、联网工具、敏感路径参数一律拒绝；
- 环境变量最小化（不透传 API Key/Token），TMPDIR 指向任务私有临时目录；
- 输出**边读边限**：超过上限立即按进程组清理，不在内存里先堆满再截断；
- 超时按进程组清理。
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.security.paths import SENSITIVE_NAMES, is_sensitive, is_within
from app.security.sandbox import SandboxPlan, available as sandbox_available
from app.security.sandbox import prepare_plan, self_check, write_profile

MAX_OUTPUT_BYTES = 256 * 1024
_READ_CHUNK = 64 * 1024

# 默认允许的可执行文件（部署方可用 commands.exec.allowlist 覆盖）
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "pytest", "python", "python3", "python3.12", "node", "npm", "pnpm", "yarn",
    "make", "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "git",
    "go", "cargo", "rustc", "mvn", "gradle", "dotnet", "ruff", "mypy", "tsc",
)

# 解释器类可执行文件本身就在白名单里，因此 `python3 -c ...` 这类写法是允许的；
# 其越界风险由沙箱（写/网络/凭据读）而不是命令名承担。
DENIED_COMMANDS: frozenset[str] = frozenset({
    "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash",
    "sudo", "su", "doas", "runas", "curl", "wget", "nc", "netcat", "ncat",
    "ssh", "scp", "sftp", "rsync", "telnet", "ftp", "docker", "podman",
    "kubectl", "mount", "umount", "diskutil", "shutdown", "reboot",
    "chown", "chmod", "mkfs", "dd", "kill", "killall", "pkill", "launchctl",
    "systemctl", "crontab", "at", "osascript", "open", "defaults",
})

_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "PYTHONPATH",
                  "VIRTUAL_ENV", "NODE_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")

SANDBOX_UNAVAILABLE_REASON = (
    "exec 需要可验证的隔离（文件写边界、禁网、凭据不可读），"
    "当前平台不支持 sandbox-exec，已禁用 exec；不会以 cwd/白名单冒充隔离。"
)


class ExecDenied(PermissionError):
    """命令被策略拒绝；message 可直接回执。"""


@dataclass(frozen=True, slots=True)
class ExecPolicy:
    """可验证的命令白名单与资源上限。"""

    allowlist: tuple[str, ...] = DEFAULT_ALLOWLIST
    denied: frozenset[str] = DENIED_COMMANDS
    max_timeout: float = 300.0
    max_output_bytes: int = MAX_OUTPUT_BYTES
    allow_network_tools: bool = False
    extra_denied: tuple[str, ...] = ()
    # 必须使用平台沙箱（默认 True；没有沙箱时 exec 整体禁用）
    require_sandbox: bool = True
    allow_network: bool = False

    def check(self, argv: Sequence[str], *, workspace: Path, cwd: Path) -> None:
        if not argv:
            raise ExecDenied("命令为空。")
        for item in argv:
            if not isinstance(item, str):
                raise ExecDenied("命令参数必须是字符串数组。")
            if "\x00" in item:
                raise ExecDenied("命令参数包含 NUL 字节。")
        executable = Path(argv[0]).name
        if executable in self.denied or executable in self.extra_denied:
            raise ExecDenied(f"命令 {executable} 在禁止清单中。")
        if not self.allow_network_tools and executable in {
            "curl", "wget", "nc", "ssh", "scp", "sftp", "rsync", "telnet",
        }:
            raise ExecDenied(f"命令 {executable} 属于联网工具，未获授权。")
        if not self._allowed(executable):
            raise ExecDenied(
                f"命令 {executable} 不在允许清单内；可由管理员配置 commands.exec.allowlist。"
            )
        if not is_within(cwd, workspace):
            raise ExecDenied("命令的工作目录超出本次任务的授权目录。")
        for item in argv[1:]:
            candidate = Path(item)
            if item.startswith("-"):
                continue
            if item.startswith("/") or item.startswith("./") or item.startswith("../"):
                try:
                    resolved = Path(os.path.realpath(str(cwd / candidate))) if not candidate.is_absolute() \
                        else Path(os.path.realpath(str(candidate)))
                except OSError:
                    continue
                if not is_within(resolved, workspace):
                    raise ExecDenied(f"参数 {item} 指向授权目录之外。")
                if is_sensitive(resolved):
                    raise ExecDenied(f"参数 {item} 命中敏感路径（凭据/密钥/数据库）。")
            if Path(item).name in SENSITIVE_NAMES:
                raise ExecDenied(f"参数 {item} 命中敏感文件名。")

    def _allowed(self, executable: str) -> bool:
        for entry in self.allowlist:
            name = Path(entry).name
            if executable == name or executable == entry:
                return True
        return False

    def env(
        self, base: Mapping[str, str] | None = None, *, temp_dir: Path | None = None
    ) -> dict[str, str]:
        source = base if base is not None else os.environ
        env = {key: value for key, value in source.items() if key in _SAFE_ENV_KEYS}
        env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        if temp_dir is not None:
            env["TMPDIR"] = str(temp_dir)
        env["QQ_EXEC"] = "1"
        return env

    def clamp_timeout(self, value: float | None) -> float:
        if value is None or value <= 0:
            return min(60.0, self.max_timeout)
        return min(float(value), self.max_timeout)


@dataclass(slots=True)
class ExecResult:
    """一次受控命令的结果。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    timed_out: bool = False
    argv: tuple[str, ...] = ()
    cwd: str = ""
    denied_reason: str = ""
    sandbox: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.denied_reason

    def summary(self, *, limit: int = 2000) -> str:
        head = f"命令：{' '.join(self.argv)}（exit={self.exit_code}，{self.duration_ms}ms）"
        body = (self.stdout or "").strip()
        err = (self.stderr or "").strip()
        parts = [head]
        if body:
            parts.append("stdout：\n" + body[:limit])
        if err:
            parts.append("stderr：\n" + err[:limit])
        if self.sandbox:
            parts.append(f"（隔离：{self.sandbox}）")
        if self.timed_out:
            parts.append("（超时，已按进程组清理）")
        if self.truncated:
            parts.append("（输出超过上限，已截断并终止进程组）")
        if self.denied_reason:
            parts.append(f"已拒绝：{self.denied_reason}")
        return "\n".join(parts)

    def as_dict(self) -> dict:
        return {
            "argv": list(self.argv), "cwd": self.cwd, "exit_code": self.exit_code,
            "stdout": self.stdout, "stderr": self.stderr,
            "duration_ms": self.duration_ms, "timed_out": self.timed_out,
            "truncated": self.truncated, "denied": self.denied_reason,
            "sandbox": self.sandbox,
        }


def isolation_status() -> str:
    """exec 隔离状态（/status 与启动日志用）。"""
    return self_check()


async def run_command(
    argv: Sequence[str], *, workspace: Path, cwd: Path, policy: ExecPolicy,
    timeout: float | None = None, protected: tuple[Path, ...] = (),
    path_rules: Sequence[tuple[Path, bool, bool]] = (),
) -> ExecResult:
    """按策略在平台沙箱内执行一次本地命令。

    `protected` 是读写都拒绝的路径；`path_rules` 是 (路径, 允许读, 允许写) 三元组，
    用于表达"更具体的目录 ACL"（例如父目录 exec、子目录只读；某用户在某子树只有 text）。
    """
    started = time.monotonic()
    try:
        policy.check(argv, workspace=workspace, cwd=cwd)
    except ExecDenied as exc:
        return ExecResult(
            exit_code=-1, argv=tuple(argv), cwd=str(cwd), denied_reason=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if policy.require_sandbox and not sandbox_available():
        return ExecResult(
            exit_code=-1, argv=tuple(argv), cwd=str(cwd),
            denied_reason=SANDBOX_UNAVAILABLE_REASON,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    workspace = Path(os.path.realpath(str(workspace)))
    cwd = Path(os.path.realpath(str(cwd)))
    limit = policy.clamp_timeout(timeout)
    proc = None
    truncated = False
    timed_out = False
    temp_handle = tempfile.TemporaryDirectory(prefix="qq-exec-")
    temp_dir = Path(os.path.realpath(temp_handle.name))
    plan: SandboxPlan | None = None
    try:
        launch = list(argv)
        if policy.require_sandbox:
            plan = prepare_plan(
                workspace=workspace, temp_dir=temp_dir, protected=protected,
                allow_network=policy.allow_network, executable=str(argv[0]),
                path_rules=tuple(path_rules),
            )
            profile = write_profile(plan, temp_dir)
            launch = plan.wrap(list(argv), profile_path=profile)
        proc = await asyncio.create_subprocess_exec(
            *launch, cwd=str(cwd), env=policy.env(temp_dir=temp_dir),
            start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr, truncated, timed_out = await _drain(
            proc, deadline=time.monotonic() + limit,
            max_bytes=policy.max_output_bytes,
        )
        if truncated or timed_out:
            await _kill_group(proc)
        await proc.wait()
        code = -9 if (truncated or timed_out) else (
            proc.returncode if proc.returncode is not None else -1
        )
        return ExecResult(
            exit_code=code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated, timed_out=timed_out, argv=tuple(argv), cwd=str(cwd),
            sandbox=(
                "sandbox-exec（读取默认拒绝，仅系统库/解释器/任务目录可读；"
                "写限任务目录 + /dev/null 空设备；禁网；凭据、密钥与受保护路径一律拒绝）"
                if plan else "none"
            ),
        )
    except FileNotFoundError:
        return ExecResult(
            exit_code=-2, argv=tuple(argv), cwd=str(cwd),
            stderr="可执行文件不存在。", duration_ms=int((time.monotonic() - started) * 1000),
        )
    except OSError as exc:
        return ExecResult(
            exit_code=-3, argv=tuple(argv), cwd=str(cwd), stderr=f"启动失败：{exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if proc is not None and proc.returncode is None:
            await _kill_group(proc)
        temp_handle.cleanup()


async def _drain(proc, *, deadline: float, max_bytes: int) -> tuple[bytes, bytes, bool, bool]:
    """增量有界读取：超限或超时立即返回并标记，由调用方清理进程组。"""
    stdout = bytearray()
    stderr = bytearray()
    state = {"truncated": False}

    async def collect(stream, sink: bytearray) -> None:
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                return
            remaining = max_bytes - (len(stdout) + len(stderr))
            if remaining <= 0:
                state["truncated"] = True
                return
            sink.extend(chunk[:remaining])
            if len(chunk) > remaining:
                state["truncated"] = True
                return

    tasks = [
        asyncio.create_task(collect(proc.stdout, stdout)),
        asyncio.create_task(collect(proc.stderr, stderr)),
    ]
    timed_out = False
    try:
        timeout = max(0.0, deadline - time.monotonic())
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return bytes(stdout), bytes(stderr), state["truncated"], timed_out


async def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def policy_from_config(raw: object) -> ExecPolicy:
    """从配置构造 ExecPolicy；未配置时使用保守默认值。"""
    if not isinstance(raw, dict):
        return ExecPolicy()
    allowlist = raw.get("allowlist")
    extra_denied = raw.get("deny") or ()
    return ExecPolicy(
        allowlist=tuple(str(item) for item in allowlist) if allowlist else DEFAULT_ALLOWLIST,
        max_timeout=float(raw.get("max_timeout_seconds") or 300.0),
        max_output_bytes=int(raw.get("max_output_bytes") or MAX_OUTPUT_BYTES),
        allow_network_tools=bool(raw.get("allow_network_tools") or False),
        extra_denied=tuple(str(item) for item in extra_denied),
        require_sandbox=bool(raw.get("require_sandbox", True)),
        allow_network=bool(raw.get("allow_network") or False),
    )
