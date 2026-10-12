"""可验证的本地命令隔离（macOS Seatbelt / sandbox-exec）。

F01 / R01 / R02 要求：exec 必须有**真正**的隔离，不能靠 cwd、命令名白名单或提示词。
本模块用 macOS 自带的 `sandbox-exec` 生成一次性 profile，对子进程强制：

- 读：**默认拒绝全部 file-read-data**，只显式放行
  * 系统/运行库路径（解释器与 dylib 需要的固定位置）；
  * 解释器自身（解析后的可执行文件、其所在目录，以及所属 venv 根）；
  * 本次任务的工作目录与任务私有临时目录；
  随后再用**敏感规则**（`.env*`、`*.pem|key|...`、`id_rsa`、凭据文件名等，与 broker 的
  `app.security.paths` 共用同一份清单）和**受保护路径**覆盖上面的放行，
  因此工作区外**任意**位置（同级、上级、嵌套）的 `.env` 与密钥都读不到，
  受保护子树也不会因为"位于授权根内部"而失效（保护优先于授权根）。
- 写：先全局 `deny file-write*`，只放行工作目录与任务私有临时目录，
  以及系统空设备**字面量** `/dev/null`（pytest logging 等合法工具需要，内核丢弃写入、
  不落任何用户文件），再对受保护子树追加写禁止；其余一律拒绝；
- 网络：`deny network*` 全禁（含本机回环），需要联网的任务必须显式改配置；
- profile 中的每个路径都先做真实路径解析（macOS 上 `/tmp` → `/private/tmp`），
  否则规则会因为符号链接别名而失配。

平台不可用或探测失败时 `available()` 返回 False，调用方必须**明确禁用 exec**，
不得退回无隔离执行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.security.paths import seatbelt_patterns
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# 系统与运行库读取白名单（解释器/动态库的固定位置）。
# 这些是"尽量小的例外"：`/private/tmp` 只是因为 macOS 上 `/tmp` 是其符号链接，
# `/private/etc`、`/private/var/db` 供 dyld 与 locale/时区使用。
SYSTEM_READ_SUBPATHS: tuple[str, ...] = (
    "/usr", "/System", "/Library", "/bin", "/sbin",
    "/dev", "/opt/homebrew", "/private/tmp", "/private/etc", "/private/var/db",
)

# 项目根下允许保持可读的目录名（解释器自身所在）。其他条目一律进入拒绝侧。
# 本项目用 `.venv`；若部署方把虚拟环境放在别处，请在这里补名（测试会提醒）。
_PROJECT_VENV_NAMES: frozenset[str] = frozenset({".venv"})

# 读取被拒时的可读说明（/status、日志与测试共用）。
READ_POLICY_SUMMARY = "读取默认拒绝；仅系统库、解释器、任务目录可读；凭据/密钥/受保护路径一律拒绝"


def _real(value: str | os.PathLike[str] | None) -> Path | None:
    """真实路径解析（失败返回 None）；规则里的路径必须真实化，否则会被别名绕过。"""
    if not value:
        return None
    try:
        return Path(os.path.realpath(str(value)))
    except OSError:  # pragma: no cover - 极端平台
        return None


@dataclass(frozen=True, slots=True)
class SandboxPlan:
    """一次 exec 的隔离参数。"""

    workspace: Path
    temp_dir: Path
    # 需在"授权放行之后"追加拒绝的路径：受保护/敏感子树（保护优先于授权根）
    read_deny: tuple[Path, ...] = field(default_factory=tuple)
    # 只拒绝写、但允许读的路径（更具体的"只读"目录 ACL，T01）
    write_deny: tuple[Path, ...] = field(default_factory=tuple)
    # 解释器自身需要的读取例外（可执行文件所在目录、所属 venv 等）
    read_allow: tuple[Path, ...] = field(default_factory=tuple)
    # 追加放行的读取根（例如执行器显式声明的只读工具目录）
    read_extra: tuple[Path, ...] = field(default_factory=tuple)
    allow_network: bool = False

    def read_allow_paths(self) -> tuple[Path, ...]:
        """任务目录与解释器例外的放行集合。

        同时包含路径本身与其真实路径：macOS 上 `/tmp/x` 与 `/private/tmp/x` 是同一位置，
        只写其中一种形态会让规则因为别名而失配（表现为"解释器跑不起来"或"工作区不可读"）。
        """
        seen: list[Path] = []
        for item in (*self.read_allow, *self.read_extra, self.workspace, self.temp_dir):
            for candidate in (Path(item), _real(item)):
                if candidate is not None and candidate not in seen:
                    seen.append(candidate)
        return tuple(seen)

    def profile(self) -> str:
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow sysctl-read)",
            "(allow signal (target self))",
            # 目录枚举/stat 需要 metadata；内容读取仍由 file-read-data 控制，
            # 因此 `ls` 能看到文件名，但打不开未授权/敏感文件的内容。
            "(allow file-read-metadata)",
            '(allow file-read-data (literal "/"))',
        ]
        for path in SYSTEM_READ_SUBPATHS:
            lines.append(f'(allow file-read-data (subpath "{path}"))')
        # 放行：任务目录、任务私有临时目录，以及解释器自身的例外（可能就装在项目内的
        # `.venv`）。SBPL 是**后匹配者优先**，因此所有 deny 都必须排在这些 allow 之后。
        allow_reads: list[Path] = []
        for item in (self.workspace, self.temp_dir, *self.read_extra, *self.read_allow_paths()):
            for candidate in (Path(item), _real(item)):
                if candidate is not None and candidate not in allow_reads:
                    allow_reads.append(candidate)
        for path in allow_reads:
            lines.append(f'(allow file-read-data (subpath "{path}"))')
        # 拒绝：受保护路径（即使位于任务目录内部也必须拒绝——保护优先于授权根，R02）、
        # 机器人自身项目目录、以及任意位置命中敏感清单的文件（R01）。
        for path in self.read_deny:
            if str(path):
                lines.append(f'(deny file-read-data (subpath "{path}"))')
        # T03：上面的 deny 可能整块覆盖解释器（默认 protected_paths 含项目根，而解释器
        # 常装在项目内的 `.venv`）。因此在所有 deny 之后**只把解释器自身的运行时路径**
        # 重新放行一次——这是"明确且尽量小的例外"：只读、且敏感清单仍然对它们生效。
        # 注意这里只传解释器例外（`self.read_allow`），**不能**把任务目录/临时目录再放一次，
        # 否则会覆盖上面的受保护/只读 deny（受保护目录可能在任务目录里）。
        # 没有这一步，通过 broker 的正常 exec 连 Python 都启动不了（Fatal Python error:
        # init_import_site），用户被迫加 `-S` 绕过，属于把功能搞坏而不是收紧权限。
        for path in interpreter_read_paths(executable=None, allow=list(self.read_allow)):
            lines.append(f'(allow file-read-data (subpath "{path}"))')
        # 敏感清单放在最后：它在**所有**放行（含上面的解释器例外）之后生效，
        # 因此 `.venv` 里出现 `.env`/密钥时同样读不到。
        for pattern in seatbelt_patterns():
            lines.append(f'(deny file-read-data (regex #"{pattern}"))')
        # 写：全局拒绝，只放行任务目录与任务临时目录（含别名形态），
        # 再收回受保护子树。解释器目录只获得**读**放行，绝不写。
        lines.append("(deny file-write*)")
        writable: list[Path] = []
        for item in (self.workspace, self.temp_dir):
            for candidate in (Path(item), _real(item)):
                if candidate is not None and candidate not in writable:
                    writable.append(candidate)
        for path in writable:
            lines.append(f'(allow file-write* (subpath "{path}"))')
        # 收回写权限：受保护子树（读写都拒绝）+ 只读子树（T01：父目录 exec、子目录只读时，
        # 子目录可读但不可写）。
        write_denies: list[Path] = []
        for item in (*self.read_deny, *self.write_deny):
            resolved = _real(item) or Path(item)
            if resolved not in write_denies:
                write_denies.append(resolved)
        for path in write_denies:
            if str(path):
                lines.append(f'(deny file-write* (subpath "{path}"))')
        if not self.allow_network:
            lines.append("(deny network*)")
        # 唯一放行的"目录外写"是系统空设备字面量 /dev/null。
        #
        # 理由（真实实测）：pytest 的 logging 插件在 `log_file` 未配置时会打开
        # `os.devnull`（`open("/dev/null", "w")`）作为日志 handler；沙箱默认 `deny file-write*`
        # 会让 pytest 在 configure 阶段以 PermissionError 内部崩溃（INTERNALERROR），
        # 于是"沙箱内跑项目 venv pytest"这一合法验收动作被误伤。写入 /dev/null 不落任何
        # 用户文件（内核丢弃全部字节），因此这是必要且最小的例外：
        # - 只匹配字面量 `/dev/null`（不用 subpath，绝不放行 /dev 下其他设备）；
        # - 放在工作目录/临时目录放行与受保护 deny 之后（SBPL 后匹配者优先），
        #   其余目录外写、敏感读、禁网策略全部保持不变（相邻/目录外哨兵测试覆盖）。
        lines.append('(allow file-write* (literal "/dev/null"))')
        return "\n".join(lines) + "\n"

    def wrap(self, argv: list[str], *, profile_path: Path) -> list[str]:
        return [SANDBOX_EXEC, "-f", str(profile_path), *argv]


class SandboxUnavailable(RuntimeError):
    """当前平台无法提供可验证隔离；调用方必须禁用 exec 而不是降级执行。"""


_AVAILABLE: bool | None = None


def executable() -> str | None:
    return SANDBOX_EXEC if Path(SANDBOX_EXEC).is_file() else shutil.which("sandbox-exec")


def available() -> bool:
    """探测 sandbox-exec 是否真的可用（结果缓存）。"""
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = _probe()
    return _AVAILABLE


def _probe() -> bool:
    binary = executable()
    if not binary:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="qq-sbx-probe-") as directory:
            profile = Path(directory) / "probe.sb"
            real = Path(os.path.realpath(directory))
            probe = real / "probe.txt"
            probe.write_text("ok", encoding="utf-8")
            plan = SandboxPlan(workspace=real, temp_dir=real)
            profile.write_text(plan.profile(), encoding="utf-8")
            completed = subprocess.run(
                [binary, "-f", str(profile), "/bin/cat", str(probe)],
                capture_output=True, timeout=10, check=False,
            )
            return completed.returncode == 0 and b"ok" in completed.stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 平台差异
        return False


def deny_read_paths(
    *, workspace: Path, protected: tuple[Path, ...] = (),
    executable: str | None = None,
) -> tuple[Path, ...]:
    """计算需要在授权放行之后**追加拒绝**的路径。

    与旧实现的关键区别（R02）：工作目录内部的受保护项**不再被删除**。保护优先于
    授权根，否则"根目录授权"会覆盖更严格的保护项。

    系统库与凭据的读取由 profile 的**默认拒绝** + 敏感正则承担，不再依赖这里逐条列举；
    这里只补"配置层面的受保护根"与"机器人自身项目目录"。
    """
    import sys

    denies: list[Path] = []

    def add(value: str | os.PathLike[str] | None) -> None:
        resolved = _real(value)
        if resolved is not None and resolved not in denies:
            denies.append(resolved)

    for path in protected:
        add(path)

    # 机器人自身项目目录：**逐项**拒绝，而不是整体拒绝。
    #
    # 为什么不整体拒绝：解释器可能就装在项目内的 `.venv`（本项目的部署方式），
    # 整体拒绝会把解释器自己挡在外面，沙箱内根本跑不起来；若再在 deny 之后重新放行
    # `.venv`，"项目目录"这条 deny 就又失效了。逐项拒绝让解释器例外保持"明确且尽量小"。
    #
    # 安全性由白名单固定：只有 `_PROJECT_VENV_NAMES` 里的名字（本项目 = `.venv`）不被拒绝，
    # 项目根一旦出现新的条目，它默认落在拒绝侧（fail-closed）；
    # `tests/test_round3_boundaries.py::test_project_root_entries_are_denied_by_default`
    # 会在出现新条目时失败，提醒实现者显式判断。
    project = Path(__file__).resolve().parents[2]
    if project.is_dir():
        for entry in sorted(project.iterdir()):
            if entry.name in _PROJECT_VENV_NAMES:
                continue
            add(entry)
    else:  # pragma: no cover - 只有源码树被搬走/删除时才会发生
        add(project)
    return tuple(denies)


def interpreter_read_paths(
    *, executable: str | None, allow: list[Path] | None = None
) -> tuple[Path, ...]:
    """**解释器运行时**必需的读取路径（T03 重放行专用，必须尽量小）。

    只返回解释器为了启动真正需要的目录：

    1. venv 根：父链上含 `pyvenv.cfg` 的目录（site-packages 与 `pyvenv.cfg` 都在里面）；
    2. venv 的 `home`/`base-prefix` 指向的基础解释器目录（标准库在那）；
    3. 解释器可执行文件所在目录（`execvp` 需要读它）。

    **刻意不返回** `PATH` 解析出的兄弟命令目录，也不返回解释器的父目录之外的层级：
    这个函数的结果会在**所有 deny 之后**重新放行一次，范围过大会把受保护目录里的内容
    一起放回来（测试里 workspace 可能就位于解释器上一级目录下）。没有 venv 配置时，
    第 1、2 条不成立，只剩可执行文件所在目录，属于"明确且尽量小"的例外。
    """
    import sys

    roots: list[Path] = list(allow or [])
    candidates: list[Path] = []
    for raw in (executable, sys.executable):
        if not raw:
            continue
        candidates.append(Path(str(raw)).absolute())
        candidates.append(Path(os.path.realpath(str(raw))))
        resolved = shutil.which(str(raw)) or shutil.which(Path(str(raw)).name)
        if resolved:
            candidates.append(Path(resolved))

    def remember(item: Path) -> None:
        item = Path(item)
        if item and item not in roots:
            roots.append(item)

    def base_prefixes(venv_root: Path) -> None:
        try:
            text = (venv_root / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() not in {"home", "base-prefix", "base-exec-prefix"}:
                continue
            value = value.strip()
            if not value:
                continue
            for candidate in (Path(value), Path(value).parent, Path(value).parent.parent):
                resolved = _real(candidate)
                if resolved is not None:
                    remember(resolved)

    for item in candidates:
        directory = item.parent
        while True:
            if (directory / "pyvenv.cfg").is_file():
                remember(directory)
                base_prefixes(directory)
                break
            if directory.parent == directory:
                break
            directory = directory.parent
        # 可执行文件所在目录：execvp 必须能读到它
        remember(item.parent)
    return tuple(roots)


def allow_read_paths(executable: str | None) -> tuple[Path, ...]:
    """解释器自身的读取例外（在 deny 之前放行）。

    覆盖三种真实形态：
    1. 直接给出的可执行文件路径与其真实路径；
    2. `PATH` 解析结果；
    3. 可执行文件所在目录、其父目录；
    4. venv 根：父链上含 `pyvenv.cfg` 的目录，以及该文件 `home` 指向的基础解释器目录
       （venv 里 `sys.prefix` 可能是基础解释器前缀，标准库就在那边）。
    解释器读不到自己就根本跑不起来，所以这一组例外是必需的；但它只放行**读**，
    不放行写，也不覆盖后面的敏感/受保护 deny。
    """
    import sys

    candidates: list[Path] = []
    for raw in (executable, sys.executable):
        if not raw:
            continue
        candidates.append(Path(str(raw)).absolute())
        candidates.append(Path(os.path.realpath(str(raw))))
        resolved = shutil.which(str(raw)) or shutil.which(Path(str(raw)).name)
        if resolved:
            candidates.append(Path(resolved))
    allows: list[Path] = []

    def add(item: Path) -> None:
        item = Path(item)
        if item and item not in allows:
            allows.append(item)

    def add_base_prefix(venv_root: Path) -> None:
        """读取 pyvenv.cfg 的 home/base 指向，放行基础解释器目录（标准库所在）。"""
        config = venv_root / "pyvenv.cfg"
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() not in {"home", "base-prefix", "base-exec-prefix"}:
                continue
            value = value.strip()
            if not value:
                continue
            for candidate in (Path(value), Path(value).parent, Path(value).parent.parent):
                resolved = _real(candidate)
                if resolved is not None:
                    add(resolved)

    for item in candidates:
        for directory in (item.parent, item.parent.parent):
            probe = Path(directory)
            while True:
                if (probe / "pyvenv.cfg").is_file():
                    add(probe)
                    add_base_prefix(probe)
                    break
                if probe.parent == probe:
                    break
                probe = probe.parent
            add(directory)
    return tuple(allows)


def prepare_plan(
    *, workspace: Path, temp_dir: Path, protected: tuple[Path, ...] = (),
    allow_network: bool = False, extra_deny: tuple[Path, ...] = (),
    executable: str | None = None,
    path_rules: tuple[tuple[Path, bool, bool], ...] = (),
) -> SandboxPlan:
    """组装一次 exec 的隔离参数。

    `path_rules` 是 (路径, 允许读, 允许写) 三元组，用来表达"更具体的目录 ACL"：
    允许位为 False 的维度会生成 deny 规则，正好覆盖授权根放行的那一块。
    """
    denies = list(
        deny_read_paths(workspace=workspace, protected=protected, executable=executable)
    )
    for item in extra_deny:
        resolved = _real(item)
        if resolved is not None and resolved not in denies:
            denies.append(resolved)
    # 规则里的路径必须都已真实化：只有真实路径才能同时命中 `/tmp/x` 与 `/private/tmp/x`
    # 这两种写法（否则受保护目录会因为别名而漏掉）。
    normalized: list[Path] = []
    for item in denies:
        resolved = _real(item) or Path(item)
        if resolved not in normalized:
            normalized.append(resolved)
    write_deny: list[Path] = []
    for rule in path_rules:
        # 接受 (路径, 允许读, 允许写) 三元组或有同样三个属性的对象（broker 用 dataclass 传参）
        if isinstance(rule, tuple):
            raw_path, allow_read, allow_write = rule
        else:
            raw_path = getattr(rule, "path")
            allow_read = bool(getattr(rule, "allow_read"))
            allow_write = bool(getattr(rule, "allow_write"))
        resolved = _real(raw_path) or Path(raw_path)
        if not allow_read and resolved not in normalized:
            normalized.append(resolved)
        if not allow_write and resolved not in write_deny:
            write_deny.append(resolved)
    allows = allow_read_paths(executable)
    return SandboxPlan(
        workspace=Path(os.path.realpath(str(workspace))),
        temp_dir=Path(os.path.realpath(str(temp_dir))),
        read_deny=tuple(normalized),
        read_allow=tuple(allows),
        write_deny=tuple(write_deny),
        allow_network=allow_network,
    )


def write_profile(plan: SandboxPlan, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    profile = directory / "exec.sb"
    profile.write_text(plan.profile(), encoding="utf-8")
    return profile


def self_check() -> str:
    """返回人类可读的隔离状态说明（用于 /status 与启动日志）。"""
    binary = executable()
    if not binary:
        return "不可用：本机没有 sandbox-exec，exec 已禁用"
    if not available():
        return "不可用：sandbox-exec 探测失败，exec 已禁用"
    return (
        f"可用：{binary}（写限于任务目录 + /dev/null 空设备；禁网；"
        "读取默认拒绝，仅系统库/解释器/任务目录可读；凭据、密钥与受保护路径一律拒绝）"
    )
