#!/usr/bin/env python
"""统一最终交付验证：A01–A22 + 五个场景 + 遗留收尾的单一入口。

定位
----
本脚本是交付收尾的**唯一执行入口**，默认完全离线：

- 假适配器（`mock_harness`，纯配置接入）/ 临时目录 / 临时 SQLite / 假 QQ 传输；
- 真实 `create_app` + lifespan + 本机临时端口 + 受控 OneBot HTTP 模拟；
- 不调用真实模型、不连接真实 QQ、不读写生产 DB、不发送任何消息。

只有显式 `--run-real` 才会加载父进程环境里的真实 Settings，并且必须同时声明
`--budget`（真实 adapter 调用次数上限，按每次调用硬计数，不为最坏轮次预留）。
真实模式仍强制覆盖 database / workspace / workdir / 端口 / Token 为临时值，
绝不触碰真实数据库或真实目录；临时工作区放在项目树之外，才能成为合法 searchable 授权根。

它做四件事（每一项都落证据）：

1. 运行完整离线 pytest（`--no-pytest` 可关）并解析真实计数；
2. 用真实装配跑 HTTP 入口：鉴权 401/403、伪造身份、幂等、目录选择续跑、
   取消终态、重启 interrupted、确认失效、Token 不泄漏；
3. 驱动 3 后端 read → edit → exec 团队 DAG（默认 mock；`--run-real` 真后端）；
4. 演练 SQLite 一致备份恢复与代码回退，输出状态/审计/还原证据 JSON。

程序化入口（供 Codex 父进程传入已加载的真实 Settings / 适配器；`harness_run` 为等价别名）::

    import sys; sys.path.insert(0, "scripts")
    from verify_final_delivery import harness_run
    code = await harness_run(settings=loaded_settings, agents=real_agents,
                             allow_real=True, budget=30)

    # settings/agents 为父进程已存在的对象：phase_real 优先复用它们（不再重复读取凭据）；
    # 注入的适配器在验证期间强制 workdir=临时工作区、native_tools=False，并逐次计入预算。

用法::

    .venv/bin/python scripts/verify_final_delivery.py                 # 离线全量
    .venv/bin/python scripts/verify_final_delivery.py --no-pytest     # 只跑装配检查
    .venv/bin/python scripts/verify_final_delivery.py --run-real --budget 30
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

PASS = "passed"
FAIL = "failed"
SKIP = "skipped"
NA = "not_applicable"
BLOCKED = "blocked"
NOT_RUN = "not_run"

TEMP_ROOT = ROOT / "outputs/验收复现/.tmp-统一最终验证"
DEFAULT_EVIDENCE = ROOT / "outputs/验收复现/统一最终验证.json"
DEFAULT_REAL_EVIDENCE = ROOT / "outputs/验收复现/统一最终验证-真实调用.json"
TASK_TERMINAL = {"succeeded", "failed", "cancelled", "partial_failed", "interrupted", "no_change"}
TEAM_ACTIVE = {"draft", "queued", "running"}

# A01–A22 对应的离线回归文件（用于报告里的覆盖映射；实际结论以运行结果为准）
A_MATRIX: dict[str, tuple[str, tuple[str, ...]]] = {
    "A01": ("新口令唯一性", ("tests/test_unified_commands.py", "tests/test_commands.py")),
    "A02": ("统一公共语法", ("tests/test_unified_commands.py", "tests/test_remediation_contracts.py")),
    "A03": ("扩展口令", ("tests/test_extensibility.py", "tests/test_commands.py")),
    "A04": ("接入新模型/harness", ("tests/test_extensibility.py", "tests/test_harness_adapters.py")),
    "A05": ("真正统一文件操作", ("tests/test_broker_files.py", "tests/test_file_write.py")),
    "A06": ("只读强约束", ("tests/test_broker_files.py", "tests/test_acceptance_probes.py")),
    "A07": ("修改及冲突", ("tests/test_file_write.py", "tests/test_remediation_contracts.py")),
    "A08": ("exec 隔离", ("tests/test_round3_boundaries.py", "tests/test_acceptance_probes.py")),
    "A09": ("路径边界", ("tests/test_round3_boundaries.py", "tests/test_broker_files.py")),
    "A10": ("模糊查找/确认", ("tests/test_workspace_and_locks.py", "tests/test_acceptance_probes.py")),
    "A11": ("目录绑定", ("tests/test_workspace_and_locks.py", "tests/test_team_plan_freeze.py")),
    "A12": ("全入口目录锁", ("tests/test_workspace_and_locks.py", "tests/test_acceptance_probes.py")),
    "A13": ("team 核心回归", ("tests/test_team_runner.py", "tests/test_team_store.py")),
    "A14": ("team 增强权限", ("tests/test_team_enhanced_permissions.py", "tests/test_team_exec_boundaries.py")),
    "A15": ("team 计划冻结", ("tests/test_team_plan_freeze.py", "tests/test_acceptance_probes.py")),
    "A16": ("team 上下文隔离", ("tests/test_team_context_isolation.py",)),
    "A17": ("team 文件重试", ("tests/test_retry_drift_confirm.py", "tests/test_acceptance_probes.py")),
    "A18": ("HTTP 统一入口", ("tests/test_api_v1.py", "tests/test_acceptance_probes.py")),
    "A19": ("HTTP 鉴权/幂等", ("tests/test_api_v1.py", "tests/test_remediation_contracts.py")),
    "A20": ("存储迁移/重启", ("tests/test_config_migration.py", "tests/test_team_integration_dispatch.py")),
    "A21": ("新旧配置迁移", ("tests/test_config_migration.py",)),
    "A22": ("交付一致性", ("tests/test_docs_consistency.py", "tests/test_acceptance_scenarios.py")),
}


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root: Path, patterns: Sequence[str] = ("*.py",)) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if not any(path.match(pattern) for pattern in patterns):
            continue
        result[str(path.relative_to(root))] = sha256_file(path)
    return result


def manifest_digest(hashes: Mapping[str, str]) -> str:
    joined = "\n".join(f"{key}:{value}" for key, value in sorted(hashes.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def redact_paths(value: Any) -> Any:
    """证据里只保留相对工作副本的路径，避免泄漏父进程真实目录。"""
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        try:
            return str(Path(value).resolve().relative_to(ROOT))
        except (ValueError, OSError):
            return value
    if isinstance(value, dict):
        return {key: redact_paths(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_paths(item) for item in value]
    return value


def require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def tail_text(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "...(截断)...\n" + text[-limit:]


async def poll_until(
    predicate: Callable[[], Any], *, timeout: float = 20.0, interval: float = 0.02,
    message: str = "等待超时",
) -> Any:
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        last = predicate()
        if asyncio.iscoroutine(last):
            last = await last
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"{message}（最后取值：{last!r}）")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sqlite_state(db_path: Path) -> dict:
    """只读快照：表计数 / integrity / schema_version。"""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {quote_ident(table)}"
            ).fetchone()[0]
            for table in tables
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        version = None
        if "schema_meta" in tables:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            version = row[0] if row else None
        return {"tables": counts, "integrity": integrity, "schema_version": version}
    finally:
        connection.close()


def sqlite_online_backup(source: Path, target: Path) -> None:
    """SQLite 在线备份 API：包含 WAL 中已提交内容，停写一致性由引擎保证。"""
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _evidence_path(value: Path | str | None, default: Path) -> Path:
    if value is None or value == "":
        return default
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (ROOT / candidate)


def scratch_root() -> Path:
    """临时工作区根：必须在受保护项目树**之外**，否则无法成为合法授权根。

    真实工作区合同（app/workspaces/configurator.py::default_protected）把机器人项目根
    整体列为受保护范围；项目树内的目录即使声明 searchable，ACL 也会拒绝授权
    （表现为 find/select 报"没有可搜索的授权根"）。因此验证脚本的临时 workspace 放到
    系统临时目录，并保持真实合同要求的 modes/admin_modes/searchable 声明——
    不改 ACL 实现，也不放开任何保护。
    """
    for base in (Path(tempfile.gettempdir()), Path("/tmp")):
        try:
            resolved = Path(os.path.realpath(str(base)))
        except OSError:  # pragma: no cover - 极端平台
            continue
        if not resolved.is_dir():
            continue
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            return Path(tempfile.mkdtemp(prefix="qq-final-verify-ws-", dir=str(resolved)))
    raise RuntimeError("找不到项目树之外的临时目录，无法配置可搜索授权根")


class RealBudgetExceeded(RuntimeError):
    """真实 adapter 调用超过声明预算：硬门禁拒绝本次调用，不静默超支。"""


class RealCallAdapter:
    """adapter 计数代理：每次 chat/chat_with_options 先过预算硬门禁，再转发调用。"""

    def __init__(self, inner: Any, *, gate: "RealCallGate", unit: str) -> None:
        self.inner = inner
        self.gate = gate
        self.unit = unit

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def _backend_name(self) -> str:
        return str(getattr(self.inner, "name", "") or type(self.inner).__name__)

    async def chat(self, messages: Sequence[Any], *, timeout: float) -> Any:
        self.gate.acquire(unit=self.unit, backend=self._backend_name())
        return await self.inner.chat(messages, timeout=timeout)

    async def chat_with_options(self, messages: Sequence[Any], *, timeout: float,
                                options: Any) -> Any:
        self.gate.acquire(unit=self.unit, backend=self._backend_name())
        return await self.inner.chat_with_options(
            messages, timeout=timeout, options=options)


class RealCallGate:
    """真实 adapter 调用计数 + 硬门禁：按**实际调用**扣减，不为最坏轮次预留。

    旧实现用 `real_used += 6` 预留"3 步 × 2 轮"的最坏预算，报告的是预留值而不是
    真实调用；broker 实际可多轮，预留既虚报又可能低估。现在每次 adapter 调用先
    acquire：预算耗尽立即抛 `RealBudgetExceeded`（硬失败），未执行的预算不计入已调用。
    """

    def __init__(self, budget: int) -> None:
        self.budget = max(0, int(budget))
        self.calls = 0
        self.exceeded = False
        self.entries: list[dict] = []

    def acquire(self, *, unit: str, backend: str) -> None:
        if self.calls >= self.budget:
            self.exceeded = True
            raise RealBudgetExceeded(
                f"真实调用预算已用尽（{self.calls}/{self.budget}）：拒绝继续调用 "
                f"{backend}（{unit}）；未执行的预算保留不计入已调用。"
            )
        self.calls += 1
        self.entries.append({"call": self.calls, "unit": unit, "backend": backend})

    def unwrap(self, adapter: Any) -> Any:
        if isinstance(adapter, RealCallAdapter) and adapter.gate is self:
            return adapter.inner
        return adapter

    def wrap(self, adapter: Any, *, unit: str) -> Any:
        if adapter is None:
            return adapter
        return RealCallAdapter(self.unwrap(adapter), gate=self, unit=unit)

    def report(self) -> dict:
        return {
            "declared_budget": self.budget,
            "budget": self.budget,
            "calls": self.calls,
            "used": self.calls,  # 兼容既有字段名；语义已是真实调用数，不是预留
            "reserved": 0,
            "remaining": max(0, self.budget - self.calls),
            "exceeded": self.exceeded,
            "units": sorted({str(item.get("unit")) for item in self.entries}),
            "entries": list(self.entries),
        }


@dataclass
class Check:
    check_id: str
    title: str
    status: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)


class Recorder:
    """记录每个检查的结论；异常一律变成可诊断的 FAIL，绝不冒充通过。"""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def record(self, check_id: str, title: str, status: str, detail: str = "",
               evidence: dict | None = None) -> Check:
        check = Check(check_id, title, status, detail, evidence or {})
        self.checks.append(check)
        line = f"[{status.upper():<14}] {check_id} {title}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)
        return check

    @contextmanager
    def step(self, check_id: str, title: str, *, applicable: bool = True,
             na_reason: str = "", skip_reason: str = ""):
        if not applicable:
            self.record(check_id, title, NA, na_reason)
            yield None
            return
        if skip_reason:
            self.record(check_id, title, SKIP, skip_reason)
            yield None
            return
        evidence: dict = {}
        try:
            yield evidence
        except Exception as exc:  # noqa: BLE001 - 记录为失败，不中断整体验证
            self.record(
                check_id, title, FAIL, f"{type(exc).__name__}: {exc}",
                {"traceback": tail_text(traceback.format_exc(), 1500),
                 **{k: v for k, v in evidence.items() if not k.startswith("_")}},
            )
        else:
            if evidence.pop("_skip", ""):
                self.record(check_id, title, SKIP, str(evidence.pop("_skip")), evidence)
            elif evidence.pop("_blocked", ""):
                self.record(check_id, title, BLOCKED, str(evidence.pop("_blocked")), evidence)
            else:
                self.record(
                    check_id, title, PASS, str(evidence.pop("_detail", "")), evidence,
                )

    @asynccontextmanager
    async def astep(self, check_id: str, title: str, *, applicable: bool = True,
                    na_reason: str = "", skip_reason: str = ""):
        if not applicable:
            self.record(check_id, title, NA, na_reason)
            yield None
            return
        if skip_reason:
            self.record(check_id, title, SKIP, skip_reason)
            yield None
            return
        evidence: dict = {}
        try:
            yield evidence
        except Exception as exc:  # noqa: BLE001
            self.record(
                check_id, title, FAIL, f"{type(exc).__name__}: {exc}",
                {"traceback": tail_text(traceback.format_exc(), 1500),
                 **{k: v for k, v in evidence.items() if not k.startswith("_")}},
            )
        else:
            if evidence.pop("_skip", ""):
                self.record(check_id, title, SKIP, str(evidence.pop("_skip")), evidence)
            elif evidence.pop("_blocked", ""):
                self.record(check_id, title, BLOCKED, str(evidence.pop("_blocked")), evidence)
            else:
                self.record(
                    check_id, title, PASS, str(evidence.pop("_detail", "")), evidence,
                )

    def counts(self) -> dict[str, int]:
        result = {PASS: 0, FAIL: 0, SKIP: 0, NA: 0, BLOCKED: 0, NOT_RUN: 0}
        for check in self.checks:
            result[check.status] = result.get(check.status, 0) + 1
        return result


# --------------------------------------------------------------------------
# 服务器与假 QQ
# --------------------------------------------------------------------------


# FastAPI 的注入类型必须能在**模块全局**解析（见 OneBotSimulator 的说明）。
# 这里保持可延迟填充：缺 fastapi 时脚本仍可导入并如实报告 BLOCKED。
Request: Any = None


def _ensure_request_type() -> None:
    global Request
    if Request is None:
        from fastapi import Request as FastAPIRequest

        Request = FastAPIRequest


class OneBotSimulator:
    """受控 OneBot HTTP API：只在本机临时端口，捕获外发消息，绝不联网。"""

    def __init__(self) -> None:
        from fastapi import FastAPI

        # FastAPI 用**路由函数的模块全局**解析字符串注解。本脚本为了在缺依赖时仍能
        # 如实报告 BLOCKED，fastapi 是延迟导入的；但 `from __future__ import annotations`
        # 会把 `request: Request` 变成字符串，若 Request 只在函数局部导入，FastAPI
        # 解析失败就把 request 当作必填 query 参数 → 所有真实 POST /send_group_msg 变 422。
        # 因此在注册路由前把真正的 Request 写进模块全局，保证注解可解析。
        _ensure_request_type()

        self.sends: list[dict] = []
        self.authorizations: list[str] = []
        app = FastAPI(title="final-verify-onebot-sim")

        @app.post("/send_group_msg")
        async def send_group_msg(request: Request) -> dict:
            payload = await request.json()
            self.sends.append(payload if isinstance(payload, dict) else {"raw": payload})
            self.authorizations.append(request.headers.get("Authorization", ""))
            return {"status": "ok", "retryable": False,
                    "data": {"message_id": len(self.sends)}}

        @app.get("/__health")
        async def health() -> dict:
            return {"status": "ok", "sent": len(self.sends)}

        self.app = app

    def texts(self) -> list[str]:
        return [str(item.get("message") or "") for item in self.sends]


class LocalServer:
    """在 127.0.0.1:0 临时端口上运行一个 ASGI 应用，可等待 started 后使用。"""

    def __init__(self, app: Any, *, name: str) -> None:
        import uvicorn

        self.name = name
        self.app = app
        self._uvicorn = uvicorn
        self.server: Any = None
        self.task: asyncio.Task | None = None
        self.sock: socket.socket | None = None
        self.port: int = 0

    async def start(self) -> "LocalServer":
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = int(self.sock.getsockname()[1])
        config = self._uvicorn.Config(
            self.app, log_level="error", access_log=False, lifespan="on",
        )
        self.server = self._uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        end = time.monotonic() + 20.0
        while not self.server.started:
            if self.task.done():
                error = None
                try:
                    error = self.task.exception()
                except Exception:  # noqa: BLE001 - 取消等异常同样作为启动失败
                    error = "cancelled"
                raise AssertionError(f"{self.name} 启动失败：{error!r}")
            if time.monotonic() > end:
                raise AssertionError(f"{self.name} 启动超时")
            await asyncio.sleep(0.01)
        return self

    async def stop(self) -> None:
        if self.server is None and self.task is None:
            return
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=20.0)
            except asyncio.TimeoutError:  # pragma: no cover - 明确记录而不是挂死
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.sock is not None:
            self.sock.close()
        self.server = None
        self.task = None
        self.sock = None


# --------------------------------------------------------------------------
# 配置装配（全部临时路径）
# --------------------------------------------------------------------------


def read_script(marker: str = "READ_DONE") -> list[Any]:
    return [
        {"actions": [{"action": "read", "path": "input.txt"}]},
        {"final": marker},
    ]


def edit_script(content: str = "EDIT_OK", target: str = "dag.txt",
                marker: str = "EDIT_DONE") -> list[Any]:
    return [
        {"actions": [{"action": "propose_changes", "changes": [
            {"path": target, "content": content}]}]},
        {"final": marker},
    ]


def exec_script(marker: str = "EXEC_DONE", output: str = "DAG_EXEC_OK") -> list[Any]:
    code = (
        "from pathlib import Path; "
        "Path('exec-marker.txt').write_text('exec-ok'); "
        f"print({output!r})"
    )
    return [
        {"actions": [{"action": "run_command",
                      "argv": ["python3", "-c", code], "timeout": 20}]},
        {"final": marker},
    ]


def text_script(marker: str = "TEXT_OK") -> list[Any]:
    return [{"final": marker}]


# 真实 DAG 的 exec 步骤：用**命令名**调用项目解释器跑 pytest（不依赖 PATH 里的全局 pytest，
# 也不把解释器的绝对路径交给模型）。实测 Codex 模型会拒绝参数里含受保护目录
# （`/Users/.../Documents/...`）的绝对解释器路径，因此解释器目录由 `interpreter_on_path()`
# 在本验证进程内前置到 PATH（见 run_verification），指令本身只出现 `python3`/`python`。
# 测试体自身写出 exec-marker.txt，使 `_run_dag` 的"必须有真实命令产物"判据成立，
# 且产物只能由真实执行（pytest 加载并运行测试函数）产生。
SCAFFOLD_TEST_NAME = "test_scaffold.py"
SCAFFOLD_TEST_SOURCE = (
    "from pathlib import Path\n\n\n"
    "def test_scaffold_pytest_runs():\n"
    "    assert 2 + 2 == 4\n"
    "    # 断言通过后才写产物：marker 存在 = pytest 收集、运行且测试通过\n"
    "    Path('exec-marker.txt').write_text('exec-ok', encoding='utf-8')\n"
)

# 允许出现在给模型的 exec 指令里的解释器命令名（绝不使用绝对路径）。
INTERPRETER_COMMAND_NAMES = ("python3", "python")


def write_scaffold_test(workspace: Path) -> Path:
    """写入脚手架测试文件（真实 exec 步骤的运行目标）。"""
    path = workspace / SCAFFOLD_TEST_NAME
    path.write_text(SCAFFOLD_TEST_SOURCE, encoding="utf-8")
    return path


def interpreter_command() -> str:
    """本次验证可用的解释器**命令名**（python3/python，不是受保护目录的绝对路径）。

    取 `sys.executable` 的名字；venv 里通常就是 `python3`/`python`。若名字是 `python3.12`
    这类版本化名字，则退回到同目录里存在的 `python3`/`python`——两者都在 exec 白名单里。
    """
    directory = Path(sys.executable).parent
    name = Path(sys.executable).name
    for candidate in (name, *INTERPRETER_COMMAND_NAMES):
        if candidate in INTERPRETER_COMMAND_NAMES and (directory / candidate).is_file():
            return candidate
    return INTERPRETER_COMMAND_NAMES[0]


def scaffold_exec_instruction() -> str:
    """真实 exec 步骤指令：PATH 上的项目解释器命令名 + `-m pytest`。

    不写解释器绝对路径（Codex 模型会拒绝含受保护目录的绝对路径），也不依赖全局
    pytest：解释器目录由 `interpreter_on_path()` 在本进程内前置到 PATH，解析到的
    仍是同一个项目 venv。
    """
    return (
        "在本目录运行脚手架测试，必须用项目 venv 的解释器命令名（不要依赖全局安装的 pytest，"
        "也不要用解释器的绝对路径）："
        f"{interpreter_command()} -m pytest -q {SCAFFOLD_TEST_NAME}"
    )


@asynccontextmanager
async def interpreter_on_path():
    """把项目解释器目录前置到本进程 PATH；退出（含异常/取消）时恢复原值。

    只覆盖本验证进程：真实 exec 步骤由本进程内的 broker 执行，`ExecPolicy.env()` 会把
    PATH 交给子进程，因此 exec 指令里的 `python3 -m pytest` 解析到的就是同一个项目 venv。
    不写用户配置、不改全局环境、不放松任何工具权限。
    """
    directory = str(Path(sys.executable).parent)
    saved = os.environ.get("PATH")
    current = saved if saved is not None else os.defpath
    entries = [entry for entry in current.split(os.pathsep) if entry]
    os.environ["PATH"] = os.pathsep.join([directory, *(entry for entry in entries if entry != directory)])
    try:
        yield directory
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


def _as_response_list(items: Sequence[Any]) -> list[str]:
    return [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in items]


def mock_backend(agent: str, responses: Sequence[Any], *, backend_id: str) -> dict:
    return {
        "adapter": "mock_harness",
        "agent": agent,
        "display_name": agent.upper(),
        "default_model": "mock-1",
        "responses": _as_response_list(responses),
    }


def offline_settings(
    base: Path, *, sim_url: str, access_token: str, api_tokens: Sequence[dict],
    responses: Mapping[str, Sequence[Any]], agents: Sequence[str] = ("deepseek", "zcode", "codex"),
    db_name: str = "bot.db", root: Path | None = None,
) -> Any:
    from app.config import Settings

    workspace = root or (base / "workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    backends = {
        f"{agent}_mock": mock_backend(agent, responses.get(agent, text_script()), backend_id=f"{agent}_mock")
        for agent in agents
    }
    return Settings(
        _env_file=None,
        qq_allowed_user_ids=[100, 101],
        qq_allowed_group_ids=[200],
        database_path=str(base / db_name),
        transport="onebot_http",
        onebot_http_api_url=sim_url,
        onebot_access_token=access_token,
        http_listen_host="127.0.0.1",
        http_listen_port=0,
        thinking_archive_dir="",
        rate_limit_per_minute=1000,
        request_timeout_seconds=30.0,
        approval_ttl_seconds=300,
        team_workflow_timeout_seconds=120.0,
        team_step_timeout_seconds=30.0,
        console_backends=backends,
        console_workspaces={
            "roots": [{
                "path": str(workspace),
                "modes": ["read", "edit", "exec"],
                "admin_modes": ["read", "edit", "exec"],
                "searchable": True,
                "label": "final-verify",
            }],
            "backup_dir": str(base / "backups"),
            "find": {"max_depth": 4, "max_candidates": 8, "max_seconds": 3.0},
        },
        console_commands={
            "exec": {"enabled": True, "require_sandbox": True, "allow_network": False},
            "broker": {"max_task_seconds": 60},
        },
        console_api={"tokens": list(api_tokens)},
    )


def api_token(name: str, token: str, user_id: int, *, scopes: Sequence[str],
              groups: Sequence[int] = (200,)) -> dict:
    return {"token": token, "name": name, "user_id": user_id,
            "group_ids": list(groups), "scopes": list(scopes)}


# --------------------------------------------------------------------------
# 通道抽象：真实 create_app(QQ over OneBot) 与 ConsoleHarness 两种装配
# --------------------------------------------------------------------------


class QQChannel:
    """真实 create_app + 本机端口 + 受控 OneBot 模拟组成的 QQ 通道。"""

    def __init__(self, client: Any, sim: OneBotSimulator, settings: Any, app: Any,
                 *, access_token: str, group_id: int = 200, user_id: int = 100,
                 self_id: int = 999) -> None:
        self.client = client
        self.sim = sim
        self.settings = settings
        self.app = app
        self.access_token = access_token
        self.group_id = group_id
        self.user_id = user_id
        self.self_id = self_id
        self.inbound: list[dict] = []
        self._seq = 0

    async def inject(self, text: str, *, user_id: int | None = None,
                     group_id: int | None = None, message_id: int | None = None,
                     token: str | None = None) -> Any:
        self._seq += 1
        payload = {
            "post_type": "message",
            "message_type": "group",
            "group_id": group_id or self.group_id,
            "user_id": user_id or self.user_id,
            "self_id": self.self_id,
            "message": text,
            "raw_message": text,
            "message_id": (message_id if message_id is not None
                           else 900000 + self._seq),
        }
        self.inbound.append(payload)
        headers = {"Authorization": "Bearer " + (token if token is not None else self.access_token)}
        return await self.client.post("/onebot/event", json=payload, headers=headers)

    async def send(self, text: str, *, timeout: float = 20.0) -> str:
        before = len(self.sim.sends)
        response = await self.inject(text)
        require(response.status_code == 200, f"事件上报失败：{response.status_code} {response.text[:200]}")
        await poll_until(lambda: len(self.sim.sends) > before, timeout=timeout,
                         message=f"没有 QQ 回执：{text[:60]}")
        await asyncio.sleep(0.2)
        return "\n".join(str(item.get("message") or "") for item in self.sim.sends[before:])

    def workflow(self, team_id: str) -> Any:
        return self.app.state.workflows.get_workflow(team_id)

    def task(self, task_id: str) -> Any:
        return self.app.state.tasks._store.get_task(task_id)  # noqa: SLF001

    def task_events(self, task_id: str, limit: int = 50) -> list:
        return self.app.state.tasks._store.events(task_id, limit=limit)  # noqa: SLF001

    def adapter_calls(self, backend_id: str) -> int:
        runtime = self.app.state.backends.get(backend_id)
        if runtime is None:
            return 0
        return len(getattr(runtime.adapter, "calls", []) or [])

    async def wait_terminal(self, team_id: str, *, timeout: float = 90.0) -> str:
        async def ready() -> Any:
            record = self.workflow(team_id)
            if record is None:
                return False
            return record.status if record.status not in TEAM_ACTIVE else False

        return await poll_until(ready, timeout=timeout, message=f"团队未收敛：{team_id}")

    async def aclose(self) -> None:
        return None


class HarnessChannel:
    """ConsoleHarness 装配（供父进程传入真实适配器映射时使用）。"""

    def __init__(self, harness: Any) -> None:
        self.harness = harness

    async def send(self, text: str, *, timeout: float = 20.0) -> str:
        return await self.harness.command(text)

    def workflow(self, team_id: str) -> Any:
        return self.harness.workflow_store.get_workflow(team_id)

    def task(self, task_id: str) -> Any:
        return self.harness.task_store.get_task(task_id)

    def task_events(self, task_id: str, limit: int = 50) -> list:
        return self.harness.task_store.events(task_id, limit=limit)

    def adapter_calls(self, backend_id: str) -> int:
        adapter = self.harness.agents.get(backend_id)
        return len(getattr(adapter, "calls", []) or []) if adapter else 0

    async def wait_terminal(self, team_id: str, *, timeout: float = 90.0) -> str:
        return await self.harness.wait_team(team_id, timeout=timeout)

    async def aclose(self) -> None:
        await self.harness.aclose()


# --------------------------------------------------------------------------
# 主验证器
# --------------------------------------------------------------------------


class FinalDeliveryVerifier:
    def __init__(
        self, *, settings: Any = None, agents: Mapping[str, Any] | None = None,
        allow_real: bool = False, budget: int | None = None,
        run_pytest: bool = True, json_out: Path | None = None,
        keep_temp: bool = False, real_settings: Any = None,
        real_evidence: Path | None = None,
    ) -> None:
        self.settings = settings
        self.agents = dict(agents) if agents else None
        self.allow_real = bool(allow_real)
        self.budget = int(budget) if budget else 0
        self.run_pytest = bool(run_pytest)
        self.json_out = (None if json_out is None
                         else _evidence_path(json_out, DEFAULT_EVIDENCE))
        self.keep_temp = bool(keep_temp)
        self.real_settings = real_settings
        self.real_evidence = _evidence_path(real_evidence, DEFAULT_REAL_EVIDENCE)
        self.force_real = False
        self.rec = Recorder()
        self.temp: Path | None = None
        # 工作区根必须位于受保护项目树之外才能成为合法 searchable 授权根（见 scratch_root）
        self.scratch: Path | None = None
        self.sim: OneBotSimulator | None = None
        self.sim_server: LocalServer | None = None
        self.gate = RealCallGate(self.budget)
        self.notes: list[str] = []
        self._servers: list[LocalServer] = []
        self.deps_ok = False

    @property
    def real_used(self) -> int:
        """真实 adapter 实际调用数（不再是预留的最坏轮次）。"""
        return self.gate.calls

    @property
    def real_entries(self) -> list[dict]:
        return self.gate.entries

    def workspace_dir(self, name: str) -> Path:
        """在项目树之外创建本阶段的工作区（可被 ACL 授权、可被 find 搜索）。"""
        assert self.scratch is not None
        path = self.scratch / name / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------- 生命周期 ----------

    async def start_app_server(self, app: Any, name: str) -> LocalServer:
        server = await LocalServer(app, name=name).start()
        self._servers.append(server)
        return server

    async def stop_app_server(self, server: LocalServer) -> None:
        await server.stop()
        if server in self._servers:
            self._servers.remove(server)

    async def __aenter__(self) -> "FinalDeliveryVerifier":
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix="run-", dir=str(TEMP_ROOT)))
        self.scratch = scratch_root()
        self.sim = OneBotSimulator()
        self.sim_server = await LocalServer(self.sim.app, name="onebot-sim").start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for server in list(self._servers):
            await server.stop()
        self._servers.clear()
        if self.sim_server is not None:
            await self.sim_server.stop()
        if self.temp is not None and not self.keep_temp:
            shutil.rmtree(self.temp, ignore_errors=True)
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)

    # ---------- 各阶段 ----------

    def phase_dependencies(self) -> None:
        """核心依赖预检：缺依赖时明确 BLOCKED，而不是把 ImportError 当业务失败。"""
        missing: list[str] = []
        for name in ("yaml", "pydantic", "pydantic_settings", "fastapi", "httpx", "uvicorn"):
            try:
                __import__(name)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{name}({type(exc).__name__})")
        self.deps_ok = not missing
        if missing:
            self.rec.record("env.dependencies", "核心依赖预检", BLOCKED,
                            "缺少依赖：" + ", ".join(missing) +
                            "；请在项目 venv 中执行，本副本如实标记为未运行")
        else:
            self.rec.record("env.dependencies", "核心依赖预检", PASS, "核心依赖齐全")

    def phase_runtime(self) -> None:
        from app.security import sandbox

        with self.rec.step("env.runtime", "运行环境与依赖") as ev:
            ev["python"] = sys.version.split()[0]
            ev["platform"] = sys.platform
            ev["sandbox"] = sandbox.self_check() if sandbox.executable() else "不可用"
            ev["sandbox_available"] = bool(sandbox.available())
            try:
                import pytest  # noqa: F401

                ev["pytest_available"] = True
            except Exception as exc:  # noqa: BLE001
                ev["pytest_available"] = False
                ev["pytest_error"] = f"{type(exc).__name__}: {exc}"
            ev["_detail"] = f"Python {ev['python']}，pytest={ev['pytest_available']}"

    def phase_temp_config(self) -> None:
        """临时 config.yaml + 不存在的 .env：证明真实 load_settings 的 YAML 通道。"""
        from app.config import load_settings

        assert self.temp is not None
        with self.rec.step("config.temp_load", "临时 config 装配（真实 load_settings）") as ev:
            temp = self.temp / "config-case"
            temp.mkdir(parents=True, exist_ok=True)
            workspace = temp / "ws"
            workspace.mkdir()
            config_path = temp / "config.yaml"
            config_path.write_text(
                "schema_version: 2\n"
                "storage:\n  database_path: %s\n" % (temp / "cfg.db") +
                "backends:\n  ds_cfg:\n    adapter: mock_harness\n    agent: deepseek\n"
                "    responses: ['{\"final\": \"CFG_OK\"}']\n"
                "workspaces:\n  roots:\n    - path: %s\n      modes: [read]\n" % workspace +
                "  backup_dir: %s\n" % (temp / "backups") +
                "server:\n  workspace_max_workers: 1\n"
                "  workspace_cross_process_lock: false\n",
                encoding="utf-8",
            )
            saved = dict(os.environ)
            try:
                os.environ.clear()
                if saved.get("PATH"):
                    os.environ["PATH"] = saved["PATH"]
                os.environ["CONFIG_FILE"] = str(config_path)
                os.environ["ENV_FILE"] = str(temp / "does-not-exist.env")
                settings = load_settings()
            finally:
                os.environ.clear()
                os.environ.update(saved)
            require(settings.schema_version == 2, f"配置 schema_version 应为 2：{settings.schema_version}")
            require("ds_cfg" in (settings.console_backends or {}), "YAML backends 未生效")
            require(settings.database_path == str(temp / "cfg.db"), "YAML database_path 未生效")
            ev["config_schema_version"] = settings.schema_version
            ev["backends"] = sorted((settings.console_backends or {}).keys())
            ev["database_path"] = redact_paths(settings.database_path)
            ev["_detail"] = "临时 YAML 覆盖生效；配置 schema_version=2"

        with self.rec.step("config.db_schema_version", "数据库滚动迁移版本（与配置版本区分）") as ev:
            from app.tasks.store import SCHEMA_VERSION, TaskStore
            from app.storage import ContextStore

            db_path = self.temp / "schema-case.db"
            store = ContextStore(str(db_path))
            try:
                tasks = TaskStore(store.connection)
                first = tasks.migrate()
                second = tasks.migrate()
                version = tasks.schema_version()
                require(version == SCHEMA_VERSION, f"任务库 schema_version={version} != {SCHEMA_VERSION}")
                require(first is None and second is None, "migrate 应可重复执行且无返回值异常")
            finally:
                store.close()
            ev["config_schema_version"] = 2
            ev["task_db_schema_version"] = version
            ev["_detail"] = f"配置 schema_version=2；任务库 schema_meta={version}（滚动加列）"

    def phase_pytest(self) -> None:
        if not self.run_pytest:
            self.rec.record("suite.pytest", "完整离线 pytest", SKIP, "由 --no-pytest 显式关闭")
            return
        with self.rec.step("suite.pytest", "完整离线 pytest（tests/）") as ev:
            try:
                import pytest  # noqa: F401
            except Exception as exc:  # noqa: BLE001
                ev["_blocked"] = f"缺少 pytest：{type(exc).__name__}: {exc}"
                return
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
                capture_output=True, text=True, timeout=3600,
            )
            output = (result.stdout or "") + (result.stderr or "")
            counts = {
                "passed": int(m.group(1)) if (m := re.search(r"(\d+) passed", output)) else 0,
                "failed": int(m.group(1)) if (m := re.search(r"(\d+) failed", output)) else 0,
                "skipped": int(m.group(1)) if (m := re.search(r"(\d+) skipped", output)) else 0,
                "errors": int(m.group(1)) if (m := re.search(r"(\d+) errors?", output)) else 0,
            }
            ev.update(counts)
            ev["exit_code"] = result.returncode
            ev["seconds"] = round(time.monotonic() - started, 1)
            ev["tail"] = tail_text(output, 1200)
            require(result.returncode == 0, f"pytest 退出码 {result.returncode}；{counts}")
            ev["_detail"] = (f"{counts['passed']} passed / {counts['failed']} failed / "
                             f"{counts['skipped']} skipped / {counts['errors']} errors")

    async def phase_http_lifecycle(self) -> dict:
        """真实 create_app + lifespan + 端口 + OneBot 模拟：HTTP/QQ 全入口。"""
        import httpx

        assert self.temp is not None and self.sim_server is not None
        base = self.temp / "http-case"
        workspace = self.workspace_dir("http-case")
        (workspace / "input.txt").write_text("FINAL_VERIFY_INPUT", encoding="utf-8")
        (workspace / "target-project").mkdir(exist_ok=True)
        (workspace / "cancel-target").mkdir(exist_ok=True)
        (workspace / "dag.txt").write_text("ORIGINAL", encoding="utf-8")

        access_token = "sim-token-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        token_main = "api-main-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        token_foreign = "api-foreign-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        token_scopeless = "api-scope-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        responses = {
            "deepseek": text_script("HTTP_TEXT_OK") + read_script("HTTP_READ_OK") * 4,
            "zcode": edit_script(),
            "codex": exec_script(),
        }
        settings = offline_settings(
            base, sim_url=f"http://127.0.0.1:{self.sim_server.port}",
            access_token=access_token,
            api_tokens=[
                api_token("main", token_main, 100, scopes=["task", "team", "workspace"]),
                api_token("foreign", token_foreign, 101, scopes=["task", "team", "workspace"]),
                api_token("scopeless", token_scopeless, 100, scopes=[]),
            ],
            responses=responses, root=workspace,
        )
        if self.settings is not None:
            # 父进程传入 Settings 时只沿用命令/执行策略；后端与路径仍取离线 mock/临时值，
            # 避免在未声明 --run-real 时误触真实模型。
            settings.console_commands = self.settings.console_commands or settings.console_commands
        from app.main import create_app

        server = await self.start_app_server(create_app(settings), "app-http")
        evidence: dict = {"port": server.port, "settings": self.settings_summary(settings)}
        evidence["workspace_contract"] = self.assert_authorized_search_root(
            settings, user_id=100, group_id=200)
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}",
                                   trust_env=False, timeout=30.0)
        channel = QQChannel(client, self.sim, settings, server.app,
                            access_token=access_token)
        headers_main = {"Authorization": "Bearer " + token_main}
        headers_foreign = {"Authorization": "Bearer " + token_foreign}
        all_response_text: list[str] = []

        def remember(response: Any) -> Any:
            all_response_text.append(response.text)
            return response

        try:
            # 1) 健康/状态/鉴权
            async with self.rec.astep("http.health_auth", "HTTP 健康与鉴权（真实 lifespan）") as ev:
                health = remember(await client.get("/healthz"))
                status = remember(await client.get("/status"))
                require(health.status_code == 200 and health.json().get("status") == "ok",
                        f"/healthz={health.status_code} {health.text[:120]}")
                require(status.status_code == 200 and status.json().get("status") == "ok",
                        f"/status={status.status_code}")
                payload = status.json()
                require(payload.get("schema_version") == 2, "配置 schema_version 应为 2")
                require(payload.get("api_v1_enabled") is True, "API 应启用")
                require(payload.get("workspaces", {}).get("cross_process_lock") is False,
                        "跨进程锁必须保持关闭（单 worker 合法交付）")
                require(payload.get("workspaces", {}).get("max_workers") == 1, "max_workers 应为 1")
                unauth = remember(await client.get("/api/v1/agents"))
                wrong = remember(await client.get("/api/v1/agents",
                                                   headers={"Authorization": "Bearer nope"}))
                good = remember(await client.get("/api/v1/agents", headers=headers_main))
                require(unauth.status_code == 401, f"无 Token 应 401：{unauth.status_code}")
                require(wrong.status_code == 401, f"错 Token 应 401：{wrong.status_code}")
                require(good.status_code == 200, f"正确 Token 应 200：{good.status_code}")
                agents = {item["name"] for item in good.json()["agents"]}
                require({"deepseek", "zcode", "codex"} <= agents, f"缺少 mock Agent：{agents}")
                ev.update({"healthz": health.status_code, "status": status.status_code,
                           "unauth": unauth.status_code, "wrong": wrong.status_code,
                           "agents": sorted(agents)})

            # 2) HTTP 鉴权/幂等/伪造身份/scope
            async with self.rec.astep("http.idempotency_authz", "HTTP 幂等 / 伪造身份 / scope / 越权") as ev:
                key = "idem-" + hashlib.sha256(os.urandom(8)).hexdigest()[:12]
                body = {"agent": "deepseek", "mode": "text", "prompt": "只返回标记",
                        "user_id": 101, "admin": True, "group_id": 200}
                before_calls = channel.adapter_calls("deepseek_mock")
                first = remember(await client.post("/api/v1/tasks", headers={**headers_main,
                                 "Idempotency-Key": key}, json=body))
                require(first.status_code == 202, f"提交应 202：{first.status_code} {first.text[:200]}")
                task_id = first.json()["task_id"]
                await poll_until(lambda: channel.task(task_id)["status"] in TASK_TERMINAL,
                                 timeout=30, message="幂等任务未收敛")
                repeat = remember(await client.post("/api/v1/tasks", headers={**headers_main,
                                   "Idempotency-Key": key}, json=body))
                require(repeat.status_code == 202, "重复提交应 202")
                require(repeat.json().get("task_id") == task_id, "同键重复提交必须同任务")
                require(repeat.json().get("idempotent") is True, "重复提交应标记 idempotent")
                calls_delta = channel.adapter_calls("deepseek_mock") - before_calls
                require(calls_delta == 1, f"同键重复不得重复调用模型：delta={calls_delta}")
                record = channel.task(task_id)
                require(record["user_id"] == 100, f"正文伪造 user_id 未被忽略：{record['user_id']}")
                forged_group = remember(await client.post(
                    "/api/v1/tasks", headers=headers_main,
                    json={**body, "group_id": 201, "prompt": "x"}))
                require(forged_group.status_code == 403, f"跨群伪造应 403：{forged_group.status_code}")
                scopeless = remember(await client.post(
                    "/api/v1/tasks", headers={"Authorization": "Bearer " + token_scopeless},
                    json={"agent": "deepseek", "mode": "text", "prompt": "x"}))
                require(scopeless.status_code == 403, f"无 scope 应 403：{scopeless.status_code}")
                foreign_view = remember(await client.get(f"/api/v1/tasks/{task_id}/result",
                                                         headers=headers_foreign))
                require(foreign_view.status_code == 404, "跨归属查询必须 404")
                require(token_main not in json.dumps(repeat.json()), "响应不得回显 Token")
                ev.update({"task_id": task_id, "calls_delta": calls_delta,
                           "forged_group": forged_group.status_code,
                           "scopeless": scopeless.status_code,
                           "foreign_view": foreign_view.status_code})

            # 3) 目录选择 + 续跑
            async with self.rec.astep("http.selection_resume", "目录选择 + 原任务续跑（find→select）") as ev:
                before_calls = channel.adapter_calls("deepseek_mock")
                created = remember(await client.post("/api/v1/tasks", headers=headers_main, json={
                    "agent": "deepseek", "prompt": "读取目标目录", "mode": "read",
                    "find": "target"}))
                require(created.status_code == 202, f"find 提交应 202：{created.status_code}")
                payload = created.json()
                require(payload.get("status") == "awaiting_selection", f"应等待选择：{payload}")
                code = payload["selection_code"]
                selection_task = payload["task_id"]
                require(channel.adapter_calls("deepseek_mock") == before_calls,
                        "选择前不得调用模型")
                foreign_select = remember(await client.post("/api/v1/workspaces/select",
                                         headers=headers_foreign,
                                         json={"code": code, "index": 1}))
                require(foreign_select.status_code == 400, "他人 select 必须拒绝")
                selected = remember(await client.post("/api/v1/workspaces/select",
                                   headers=headers_main, json={"code": code, "index": 1}))
                require(selected.status_code == 200, f"select 应 200：{selected.status_code} {selected.text[:200]}")
                require(selected.json().get("resumed_task_id") == selection_task, "必须续跑原任务")
                await poll_until(lambda: channel.task(selection_task)["status"] in TASK_TERMINAL,
                                 timeout=30, message="续跑任务未收敛")
                require(channel.adapter_calls("deepseek_mock") > before_calls, "续跑未调用模型")
                replay = remember(await client.post("/api/v1/workspaces/select",
                                  headers=headers_main, json={"code": code, "index": 1}))
                require(replay.status_code == 400, f"重复 select 必须失效：{replay.status_code}")
                ev.update({"task_id": selection_task, "code": code,
                           "foreign_select": foreign_select.status_code,
                           "replay_select": replay.status_code,
                           "final_status": channel.task(selection_task)["status"]})

            # 4) 取消终态 + 晚到确认不得复活
            async with self.rec.astep("http.cancel_terminal", "取消终态 / 晚到确认不复活") as ev:
                created = remember(await client.post("/api/v1/tasks", headers=headers_main, json={
                    "agent": "deepseek", "prompt": "取消目标目录", "mode": "read",
                    "find": "cancel"}))
                payload = created.json()
                require(payload.get("status") == "awaiting_selection", f"应等待选择：{payload}")
                cancel_task = payload["task_id"]
                cancel_code = payload["selection_code"]
                calls_before_cancel = channel.adapter_calls("deepseek_mock")
                cancelled = remember(await client.post(f"/api/v1/tasks/{cancel_task}/cancel",
                                      headers=headers_main))
                require(cancelled.status_code == 200, "取消应 200")
                await poll_until(lambda: channel.task(cancel_task)["status"] == "cancelled",
                                 timeout=15, message="任务未进入 cancelled")
                # 晚到确认：确认码可能已被取消流程回收（400），也可能被消费但续跑拒绝；
                # 两种情况都必须保持 cancelled 且不得有任何模型调用。
                late_select = remember(await client.post("/api/v1/workspaces/select",
                                       headers=headers_main,
                                       json={"code": cancel_code, "index": 1}))
                require(late_select.status_code in (200, 400),
                        f"晚到确认应被拒绝或消费：{late_select.status_code}")
                await asyncio.sleep(0.2)
                require(channel.task(cancel_task)["status"] == "cancelled",
                        "晚到确认不得复活已取消任务")
                require(channel.adapter_calls("deepseek_mock") == calls_before_cancel,
                        "晚到确认不得触发模型调用")
                again = remember(await client.post(f"/api/v1/tasks/{cancel_task}/cancel",
                                 headers=headers_main))
                require(again.status_code == 200, "重复取消应幂等返回")
                require(channel.task(cancel_task)["status"] == "cancelled", "终态不得被改写")
                ev.update({"task_id": cancel_task, "late_select": late_select.status_code,
                           "repeat_cancel": again.status_code,
                           "calls_delta": channel.adapter_calls("deepseek_mock") - calls_before_cancel})

            # 5) OneBot 入口：真实外发、鉴权、自身消息忽略、旧口令零副作用
            async with self.rec.astep("onebot.inbound_outbound", "OneBot 入口/出口与旧口令零副作用") as ev:
                before_calls = channel.adapter_calls("deepseek_mock")
                help_reply = await channel.send("/help")
                require("/task" in help_reply, f"/help 未回执：{help_reply[:120]}")
                bad = await channel.inject("/help", token="wrong-token")
                require(bad.status_code == 401, f"错误 access_token 应 401：{bad.status_code}")
                sends_before = len(self.sim.sends)
                await channel.inject("/help", user_id=channel.self_id)
                await asyncio.sleep(0.2)
                require(len(self.sim.sends) == sends_before, "自身消息必须忽略")
                legacy = await channel.send("/read something")
                require("迁移" in legacy or "旧" in legacy, f"旧口令应有迁移提示：{legacy[:120]}")
                require(channel.adapter_calls("deepseek_mock") == before_calls,
                        "旧口令不得调用模型")
                ev.update({"bad_token": bad.status_code, "legacy_reply": legacy[:120],
                           "sim_sends": len(self.sim.sends)})

            # 6) 为重启阶段准备：冻结一个未运行团队 + 一个 QQ 待选择任务
            async with self.rec.astep("restart.prepare", "重启前状态准备（冻结未运行团队/待选择任务）") as ev:
                created = await channel.send("/team create 重启恢复目标")
                match = re.search(r"t[0-9a-f]{8}", created)
                require(match, f"未取得 team id：{created[:200]}")
                restart_team = match.group(0)
                added = await channel.send(
                    f'/team add {restart_team} s1 deepseek --mode read --dir "{workspace}" -- 读取 input.txt')
                require("已添加步骤" in added, f"添加步骤失败：{added[:200]}")
                snapshot_round = self.freeze_team(server.app, restart_team)
                require(snapshot_round == 1, f"freeze_run 应进入第 1 轮：{snapshot_round}")
                find_text = '/task deepseek --find "target" --mode read -- 重启后失效'
                reply = await channel.send(find_text)
                code_match = re.search(r"/workspace select ([0-9A-Za-z]+)", reply)
                require(code_match, f"未取得选择确认码：{reply[:200]}")
                pending_code = code_match.group(1)
                recent = server.app.state.tasks._store.list_recent(  # noqa: SLF001
                    group_id=200, user_id=100, limit=20)
                pending_rows = [row for row in recent if row["status"] == "awaiting_selection"]
                require(pending_rows, f"QQ find 未落 awaiting_selection：{recent[:3]}")
                pending_task_id = pending_rows[0]["task_id"]
                ev.update({"team_id": restart_team, "round": snapshot_round,
                           "pending_task": pending_task_id, "pending_code": pending_code,
                           "find_text": find_text})
                evidence["restart"] = {
                    "team_id": restart_team,
                    "pending_task_id": pending_task_id,
                    "pending_code": pending_code,
                    "workspace": str(workspace),
                }
                evidence["replay"] = {
                    "text": find_text,
                    "message_id": channel.inbound[-1]["message_id"],
                }
        finally:
            await client.aclose()

        # 7) Token 不泄漏（汇总所有响应）
        with self.rec.step("http.token_no_leak", "Token/密钥不返回") as ev:
            blob = "\n".join(all_response_text)
            for name, value in (("api", token_main), ("foreign", token_foreign),
                                ("scopeless", token_scopeless), ("onebot", access_token)):
                require(value not in blob, f"{name} Token 出现在 HTTP 响应中")
            ev["responses_scanned"] = len(all_response_text)
            ev["_detail"] = f"扫描 {len(all_response_text)} 个响应，无 Token 回显"

        evidence["http_server"] = server
        evidence["http_settings"] = settings
        evidence["http_access_token"] = access_token
        evidence["http_tokens"] = {"main": token_main, "foreign": token_foreign,
                                   "scopeless": token_scopeless}
        evidence["http_channel"] = channel
        return evidence

    @staticmethod
    def settings_summary(settings: Any) -> dict:
        backends = settings.console_backends or {}
        return {
            "schema_version": settings.schema_version,
            "transport": settings.transport,
            "backend_ids": sorted(backends.keys()),
            "agents": sorted({str(value.get("agent") or key)
                              for key, value in backends.items()}),
            "database_path": redact_paths(settings.database_path),
            "workspace_roots": redact_paths([
                root.get("path") for root in (settings.console_workspaces or {}).get("roots", [])
            ]),
            "api_token_count": len((settings.console_api or {}).get("tokens") or []),
            "max_workers": settings.workspace_max_workers,
            "cross_process_lock": settings.workspace_cross_process_lock,
            "exec_enabled": bool(((settings.console_commands or {}).get("exec") or {}).get("enabled", True)),
        }

    @staticmethod
    def assert_authorized_search_root(settings: Any, *, user_id: int, group_id: int) -> dict:
        """按真实工作区合同校验临时根：searchable 且目标成员确有 read 授权。

        不修改 ACL 实现：根必须位于受保护项目树之外（否则 build_workspace_config 的
        protected 会优先拒绝），并显式声明 modes/admin_modes/searchable。
        """
        from app.security.acl import AclService
        from app.security.authz import Authorizer
        from app.security.paths import is_within
        from app.workspaces.configurator import build_workspace_config

        config = build_workspace_config(settings)
        acl = AclService(
            authz=Authorizer(settings.qq_allowed_user_ids, settings.qq_allowed_group_ids),
            grants=config.grants, protected_roots=config.protected,
            exec_enabled=config.exec_enabled, edit_enabled=config.edit_enabled,
        )
        roots = acl.search_grants()
        require(roots, "临时工作区不是可搜索授权根（searchable/保护范围不符合真实合同）")
        decision = acl.authorize_path(user_id=user_id, group_id=group_id,
                                      path=roots[0].path, mode="read")
        require(decision.allowed,
                f"临时工作区未获 read 授权：{decision.reason}")
        require(not any(is_within(roots[0].path, item) for item in config.protected),
                "临时工作区落在受保护范围内，授权根不会生效")
        return {
            "root": redact_paths(roots[0].path),
            "modes": sorted(decision.granted_modes),
            "searchable_roots": len(roots),
        }

    def install_real_adapters(self, app: Any, workspace: Path) -> list[str]:
        """接入父进程已加载的真实 adapter，并给所有 adapter 加预算硬门禁。

        - 只按 agent/backend 名同名校验后替换，避免把适配器接错后端；
        - 注入与自建 adapter 一律强制 workdir=临时工作区、native_tools=False
          （验证过程不放开 Codex 原生工具，也不改生产配置）；
        - 替换时继承 registry 计算出的 granted_modes，保持能力声明一致；
        - 返回被替换的 backend id 列表（写入证据）。
        """
        backends = getattr(getattr(app, "state", None), "backends", None)
        if backends is None:
            return []
        injected: list[str] = []
        for runtime in backends.backends.values():
            original = self.gate.unwrap(runtime.adapter)
            candidate = original
            if self.agents:
                for key in (runtime.agent, runtime.id):
                    if key in self.agents:
                        candidate = self.gate.unwrap(self.agents[key])
                        break
            if candidate is not original:
                granted = getattr(original, "granted_modes", None)
                if granted:
                    candidate.granted_modes = granted
                injected.append(runtime.id)
            if hasattr(candidate, "workdir"):
                candidate.workdir = str(workspace)
            candidate.runtime_options = {
                **(getattr(candidate, "runtime_options", {}) or {}), "native_tools": False,
            }
            runtime.adapter = self.gate.wrap(candidate, unit="real.app")
        return injected

    @staticmethod
    def freeze_team(app: Any, team_id: str) -> int | None:
        """把草稿团队冻结为第 1 轮（等价 /team run 的冻结步骤，但不启动执行）。"""
        from app.workflows.models import FrozenChoice, FrozenStep, PlanSnapshot

        store = app.state.workflows
        record = store.get_workflow(team_id)
        require(record is not None, f"团队不存在：{team_id}")
        steps = store.list_steps(team_id)
        require(steps, "团队没有步骤")
        frozen = tuple(
            FrozenStep(
                step_id=step.step_id, seq=step.seq, agent=step.agent,
                instruction=step.instruction, depends_on=tuple(step.depends_on),
                choice=FrozenChoice(model=step.model or "mock-1", effort=step.effort,
                                    effort_set=step.effort_set),
                mode=step.mode, backend=step.backend, workspace=step.workspace,
            )
            for step in steps
        )
        snapshot = PlanSnapshot(team_id=team_id, goal=record.goal, steps=frozen)
        return store.freeze_run(team_id, snapshot)

    async def phase_restart(self, lifecycle: dict) -> None:
        """重启：interrupted 标记、不自动调用模型、旧确认失效、晚到事件不覆盖终态、显式 retry。"""
        import httpx

        if "restart" not in lifecycle or "replay" not in lifecycle:
            # restart.prepare 失败时不再抛 KeyError 中断整个验证：如实标记本阶段阻塞，
            # 让后续 HTTP DAG / 恢复演练 / 真实阶段仍能执行并各自落证据。
            self.rec.record("restart.interrupted", "重启恢复：interrupted 且不自动调用模型",
                            BLOCKED, "restart.prepare 未完成（缺少重启前状态），本阶段跳过")
            return
        restart = lifecycle["restart"]
        replay_info = lifecycle["replay"]
        settings = lifecycle["http_settings"]
        access_token = lifecycle["http_access_token"]
        tokens = lifecycle["http_tokens"]
        workspace = Path(restart["workspace"])
        # 先停掉第一个实例（lifespan 退出、DB 落定），再做重启前的状态快照
        await self.stop_app_server(lifecycle["http_server"])
        state_before = sqlite_state(Path(settings.database_path))
        responses = {
            "deepseek": read_script("RETRY_OK") + read_script("HTTP_READ_OK"),
            "zcode": edit_script(),
            "codex": exec_script(),
        }
        restarted = offline_settings(
            Path(settings.database_path).parent, sim_url=f"http://127.0.0.1:{self.sim_server.port}",
            access_token=access_token,
            api_tokens=[
                api_token("main", tokens["main"], 100, scopes=["task", "team", "workspace"]),
                api_token("foreign", tokens["foreign"], 101, scopes=["task", "team", "workspace"]),
            ],
            responses=responses, root=workspace,
            db_name=Path(settings.database_path).name,
        )
        from app.main import create_app

        server = await self.start_app_server(create_app(restarted), "app-restart")
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}",
                                   trust_env=False, timeout=30.0)
        channel = QQChannel(client, self.sim, restarted, server.app,
                            access_token=access_token)
        headers_main = {"Authorization": "Bearer " + tokens["main"]}
        try:
            async with self.rec.astep("restart.interrupted",
                                      "重启恢复：interrupted 且不自动调用模型") as ev:
                workflow = channel.workflow(restart["team_id"])
                require(workflow is not None, "重启后团队丢失")
                require(workflow.status == "interrupted",
                        f"团队应标记 interrupted：{workflow.status}")
                step = server.app.state.workflows.get_step_run(restart["team_id"], 1, "s1")
                require(step is not None and step.status == "interrupted",
                        f"步骤应 interrupted：{getattr(step, 'status', None)}")
                pending = channel.task(restart["pending_task_id"])
                require(pending is not None and pending["status"] == "interrupted",
                        f"未完成任务应 interrupted：{pending and pending['status']}")
                require(channel.adapter_calls("deepseek_mock") == 0,
                        "重启不得自动调用模型")
                require(server.app.state.workflow_runner.active_teams == (),
                        "重启后不得有活动团队")
                ev.update({"team_status": workflow.status, "step_status": step.status,
                           "task_status": pending["status"],
                           "calls_after_start": channel.adapter_calls("deepseek_mock"),
                           "state_before": state_before["tables"].get("team_workflows")})

            async with self.rec.astep("restart.confirm_invalid",
                                      "重启后旧确认码失效 / 晚到事件不覆盖终态") as ev:
                stale = await client.post("/api/v1/workspaces/select", headers=headers_main,
                                          json={"code": restart["pending_code"], "index": 1})
                require(stale.status_code == 400, f"旧确认码必须失效：{stale.status_code}")
                require(channel.adapter_calls("deepseek_mock") == 0,
                        "失效确认不得触发模型调用")
                # 晚到事件：重放同一条 message_id 的原始 QQ 提交
                before_tasks = sqlite_state(Path(restarted.database_path))["tables"].get("tasks")
                replay = await channel.inject(replay_info["text"],
                                              message_id=replay_info["message_id"])
                require(replay.status_code == 200, "重放事件应被接受但不执行")
                await asyncio.sleep(0.3)
                after_tasks = sqlite_state(Path(restarted.database_path))["tables"].get("tasks")
                require(after_tasks == before_tasks,
                        f"晚到重放不得新建任务：{before_tasks} -> {after_tasks}")
                require(channel.task(restart["pending_task_id"])["status"] == "interrupted",
                        "晚到事件不得覆盖终态")
                ev.update({"stale_select": stale.status_code, "tasks_before": before_tasks,
                           "tasks_after": after_tasks,
                           "replayed_message_id": replay_info["message_id"]})

            async with self.rec.astep("restart.retry_recovers",
                                      "重启后显式 retry 恢复（复用/第 2 轮）") as ev:
                reply = await channel.send(f"/team retry {restart['team_id']}")
                require("第 2 轮" in reply, f"retry 未进入第 2 轮：{reply[:200]}")
                status = await channel.wait_terminal(restart["team_id"], timeout=90)
                require(status == "succeeded", f"retry 后团队状态：{status}")
                calls = channel.adapter_calls("deepseek_mock")
                require(calls > 0, "retry 未调用模型")
                result = await channel.send(f"/team result {restart['team_id']} s1")
                require("RETRY_OK" in result, f"结果未包含步骤答复：{result[:200]}")
                ev.update({"status": status, "calls": calls, "result_excerpt": result[:200]})
        finally:
            await client.aclose()
            await self.stop_app_server(server)

    async def phase_adapter_dag(self) -> None:
        """父进程传入**真实**适配器时：用 ConsoleHarness 装配驱动同一套 DAG。

        该路径会产生真实模型调用，因此与 `--run-real` 一样必须显式授权；
        预算由 `RealCallGate` 按每次实际 adapter 调用硬计数（不做最坏轮次预留）。
        """
        assert self.temp is not None
        from helpers import ConsoleHarness

        if not self.allow_real:
            self.rec.record("dag.adapter", "父进程适配器 DAG", NOT_RUN,
                            "传入真实适配器时需显式 --run-real 才执行")
            return
        if not self.agents:
            self.rec.record("dag.adapter", "父进程适配器 DAG", NOT_RUN,
                            "未传入父进程适配器：无法在不重建凭据的前提下执行")
            return
        base = self.temp / "adapter-case"
        workspace = self.workspace_dir("adapter-case")
        (workspace / "input.txt").write_text("ADAPTER_DAG_INPUT", encoding="utf-8")
        (workspace / "dag.txt").write_text("ORIGINAL", encoding="utf-8")
        write_scaffold_test(workspace)
        settings = offline_settings(
            base, sim_url="http://127.0.0.1:1", access_token="unused",
            api_tokens=[], responses={}, root=workspace,
        )
        # 安全护栏：注入的真实适配器也强制指向临时工作区、保持原生工具关闭，
        # 并统一加真实调用计数 + 预算硬门禁。
        wrapped: dict[str, Any] = {}
        for name, adapter in self.agents.items():
            if hasattr(adapter, "workdir"):
                adapter.workdir = str(workspace)
            adapter.runtime_options = {
                **(getattr(adapter, "runtime_options", {}) or {}), "native_tools": False,
            }
            wrapped[name] = self.gate.wrap(adapter, unit="dag.adapter")
        harness = ConsoleHarness(settings, wrapped)
        channel = HarnessChannel(harness)
        try:
            await self._run_dag(channel, workspace, phase="adapter",
                                agents=tuple(self.agents.keys()),
                                instructions={"exec": scaffold_exec_instruction()},
                                pytest_scaffold=True)
        finally:
            await harness.aclose()

    async def _run_dag(self, channel: Any, workspace: Path, *, phase: str,
                       agents: Sequence[str] = ("deepseek", "zcode", "codex"),
                       instructions: Mapping[str, str] | None = None,
                       pytest_scaffold: bool = False) -> dict:
        from app.security import sandbox

        read_agent, edit_agent, exec_agent = (list(agents) + ["deepseek", "zcode", "codex"])[:3]
        exec_ok = bool(sandbox.available())
        check_id = f"dag.{phase}"
        ev: dict = {}
        text = dict(instructions or {})
        async with self.rec.astep(check_id, f"3 后端 read→edit→exec DAG（{phase}）") as recorded:
            ev = recorded
            if not exec_ok:
                ev["exec_platform"] = sandbox.self_check()
            before = {path.name: sha256_file(path) for path in workspace.iterdir() if path.is_file()}
            created = await channel.send("/team create 三后端 DAG 验证")
            match = re.search(r"t[0-9a-f]{8}", created)
            require(match, f"未取得 team id：{created[:200]}")
            team_id = match.group(0)
            added = await channel.send(
                f'/team add {team_id} s1 {read_agent} --mode read --dir "{workspace}" -- '
                + text.get("read", "读取 input.txt 的内容"))
            require("已添加步骤" in added, f"read 步骤未添加：{added[:160]}")
            added = await channel.send(
                f'/team add {team_id} s2 {edit_agent} --after s1 --mode edit --dir "{workspace}" -- '
                + text.get("edit", "把 dag.txt 的完整内容改为 EDIT_OK"))
            require("已添加步骤" in added, f"edit 步骤未添加：{added[:160]}")
            if exec_ok:
                added = await channel.send(
                    f'/team add {team_id} s3 {exec_agent} --after s2 --mode exec --dir "{workspace}" -- '
                    + text.get("exec", "运行 python3 命令打印 DAG_EXEC_OK"))
                require("已添加步骤" in added, f"exec 步骤未添加：{added[:160]}")
            plan = await channel.send(f"/team plan {team_id}")
            require("s1" in plan and "s2" in plan, f"计划未展示步骤：{plan[:200]}")
            run_reply = await channel.send(f"/team run {team_id}")
            require("已排入后台执行" in run_reply, f"run 未启动：{run_reply[:200]}")
            status = await channel.wait_terminal(team_id, timeout=120)
            require(status == "succeeded", f"团队终态 {status}")
            dag_text = (workspace / "dag.txt").read_text(encoding="utf-8")
            require(dag_text == "EDIT_OK", f"edit 未真正落盘：{dag_text!r}")
            marker = workspace / "exec-marker.txt"
            pytest_cache = workspace / ".pytest_cache"
            if exec_ok:
                require(marker.is_file() and marker.read_text() == "exec-ok",
                        "exec 命令未真正执行（无产物）")
                if pytest_scaffold:
                    # 真实 exec 必须用项目解释器跑 pytest：缓存目录是 pytest 真实运行的产物，
                    # 也是 /dev/null 日志初始化修复在沙箱内生效的旁证。
                    require(pytest_cache.is_dir(),
                            "沙箱内 pytest 未真正运行（缺少 .pytest_cache 产物）")
            read_result = await channel.send(
                f"/team result {team_id} {'s3' if exec_ok else 's2'}")
            if pytest_scaffold and exec_ok:
                # 真实 pytest 步骤：硬证据是 marker + .pytest_cache（见上），
                # 这里只要求模型给出了非空执行结论，不用自然语言措辞判成败。
                require(read_result.strip(), "结果为空：模型未给出脚手架测试的执行结论")
            else:
                require("DONE" in read_result or "OK" in read_result,
                        f"结果未包含终答标记：{read_result[:200]}")
            final_names = {path.name for path in workspace.iterdir() if path.is_file()}
            record = channel.workflow(team_id)
            ev.update({
                "team_id": team_id,
                "status": status,
                "dag_text": dag_text,
                "exec_marker": marker.is_file() if exec_ok else None,
                "pytest_cache": pytest_cache.is_dir() if (exec_ok and pytest_scaffold) else None,
                "before_hashes": before,
                "final_files": sorted(final_names),
                "plan_excerpt": plan[:300],
                "result_excerpt": read_result[:300],
                "round": getattr(record, "round", None),
            })
            if not exec_ok:
                ev["_skip"] = f"本机无平台隔离，exec 按设计禁用：{sandbox.self_check()}"
        return ev

    async def phase_http_dag(self, lifecycle: dict) -> None:
        """真实 create_app（HTTP 案例的装配）驱动 mock 3 后端 DAG。"""
        import httpx

        settings = lifecycle["http_settings"]
        access_token = lifecycle["http_access_token"]
        base = self.temp / "dag-case"
        workspace = self.workspace_dir("dag-case")
        (workspace / "input.txt").write_text("DAG_INPUT", encoding="utf-8")
        (workspace / "dag.txt").write_text("ORIGINAL", encoding="utf-8")
        responses = {
            "deepseek": read_script("DAG_READ_DONE"),
            "zcode": edit_script(),
            "codex": exec_script(),
        }
        dag_settings = offline_settings(
            base, sim_url=f"http://127.0.0.1:{self.sim_server.port}",
            access_token=access_token,
            api_tokens=[api_token("main", lifecycle["http_tokens"]["main"], 100,
                                  scopes=["task", "team", "workspace"])],
            responses=responses, root=workspace,
        )
        from app.main import create_app

        server = await self.start_app_server(create_app(dag_settings), "app-dag")
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}",
                                   trust_env=False, timeout=30.0)
        channel = QQChannel(client, self.sim, dag_settings, server.app,
                            access_token=access_token)
        try:
            dag_ev = await self._run_dag(channel, workspace, phase="http")
            with self.rec.step("dag.audit", "DAG 状态/审计/变更落库") as ev:
                team_id = dag_ev.get("team_id")
                require(team_id, "缺少 DAG team_id")
                db = Path(dag_settings.database_path)
                state = sqlite_state(db)
                require(state["integrity"] == "ok", f"DB integrity={state['integrity']}")
                require(state["tables"].get("team_events", 0) > 0, "缺少 team 审计事件")
                require(state["tables"].get("file_changes", 0) >= 1, "缺少文件变更审计")
                task_rows = self.dag_task_rows(db, team_id)
                require(task_rows and all(row["operation"] for row in task_rows),
                        f"步骤缺少操作回执：{task_rows}")
                ev.update({"db_tables": state["tables"], "schema_version": state["schema_version"],
                           "task_rows": task_rows})
        finally:
            await client.aclose()
            await self.stop_app_server(server)

    @staticmethod
    def dag_task_rows(db_path: Path, team_id: str) -> list[dict]:
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT sr.step_id, sr.status, sr.task_id, t.operation, t.action_counts, "
                "t.result_text IS NOT NULL AND t.result_text != '' AS has_result "
                "FROM team_step_runs sr LEFT JOIN tasks t ON t.task_id = sr.task_id "
                "WHERE sr.team_id=? AND sr.round=1 ORDER BY sr.step_id",
                (team_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def phase_recovery(self, lifecycle: dict) -> None:
        """SQLite 一致备份恢复 + 代码回退演练（全部在副本内临时路径）。"""
        from app.storage import ContextStore
        from app.tasks.store import TaskStore

        assert self.temp is not None
        with self.rec.step("recovery.sqlite", "SQLite 一致备份/恢复演练") as ev:
            base = self.temp / "recovery-case"
            base.mkdir(parents=True, exist_ok=True)
            source = base / "source.db"
            store = ContextStore(str(source))
            try:
                TaskStore(store.connection).migrate()
                for index in range(5):
                    store.connection.execute(
                        "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, prompt, "
                        "status, idempotency_key, created_at, updated_at, mode, effort_set, key_scheme) "
                        "VALUES (?, 'qq', 200, 100, 'deepseek', ?, 'succeeded', NULL, ?, ?, 'text', 0, NULL)",
                        (f"t{index:08x}", f"行 {index}", time.time(), time.time()),
                    )
                store.connection.commit()
            finally:
                store.close()
            frozen = sqlite_state(source)
            backup = base / "consistent-backup.db"
            sqlite_online_backup(source, backup)
            backed_up = sqlite_state(backup)
            require(backed_up["integrity"] == "ok", "备份 integrity 异常")
            require(backed_up["tables"] == frozen["tables"], "备份行数不一致")
            require(backed_up["schema_version"] == frozen["schema_version"], "备份 schema 版本不一致")
            # 停写后制造“后来新增的数据”，再从备份恢复：新数据必须消失、旧数据一致
            connection = sqlite3.connect(str(source))
            try:
                connection.execute(
                    "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, prompt, status, "
                    "created_at, updated_at, mode, effort_set) "
                    "VALUES ('tdeadbeef', 'qq', 200, 100, 'deepseek', '事故后新增', 'succeeded', ?, ?, 'text', 0)",
                    (time.time(), time.time()),
                )
                connection.commit()
            finally:
                connection.close()
            mutated = sqlite_state(source)
            require(mutated["tables"]["tasks"] == frozen["tables"]["tasks"] + 1, "变更未生效")
            restored = base / "restored.db"
            shutil.copyfile(backup, restored)
            restored_state = sqlite_state(restored)
            require(restored_state["tables"] == frozen["tables"], "恢复后行数不等于备份时点")
            require(restored_state["integrity"] == "ok", "恢复后 integrity 异常")
            sentinel = sqlite3.connect(str(restored)).execute(
                "SELECT COUNT(*) FROM tasks WHERE task_id='tdeadbeef'"
            ).fetchone()[0]
            require(sentinel == 0, "恢复后仍存在备份之后的新数据")
            ev.update({
                "source_tables": frozen["tables"],
                "mutated_tasks": mutated["tables"]["tasks"],
                "restored_tables": restored_state["tables"],
                "backup_sha256": sha256_file(backup),
                "integrity": restored_state["integrity"],
                "schema_version": restored_state["schema_version"],
                "backup_mode": "sqlite3 Connection.backup（含 WAL 已提交内容）",
            })
            ev["_detail"] = (f"备份/恢复行数一致（tasks={frozen['tables']['tasks']}），"
                             f"恢复回退事故后新增数据")

        with self.rec.step("recovery.code_rollback", "代码快照/漂移检测/回退演练") as ev:
            frozen_tree = self.temp / "frozen-source"
            copy_tree(ROOT / "app", frozen_tree / "app")
            frozen_hashes = tree_hashes(frozen_tree / "app")
            live_hashes = tree_hashes(ROOT / "app")
            require(frozen_hashes == live_hashes, "快照与工作副本源码不一致")
            candidate = self.temp / "candidate-source"
            copy_tree(frozen_tree / "app", candidate / "app")
            target = candidate / "app/backends/broker_loop.py"
            with target.open("a", encoding="utf-8") as handle:
                handle.write("\n# 回退演练：候选改动\n")
            require(tree_hashes(candidate / "app") != frozen_hashes, "候选漂移未被检测到")
            copy_tree(frozen_tree / "app", candidate / "app")
            rolled = tree_hashes(candidate / "app")
            require(rolled == frozen_hashes, "回退后哈希不等于冻结版本")
            delivery_trees = {
                name: tree_hashes(ROOT / name) for name in ("app", "tests", "scripts")
            }
            ev.update({
                "app_files": len(frozen_hashes),
                "app_manifest_sha256": manifest_digest(frozen_hashes),
                "delivery_manifests": {
                    name: {"files": len(hashes), "sha256": manifest_digest(hashes)}
                    for name, hashes in delivery_trees.items()
                },
                "rollback_equal": True,
                "scope_note": "仅演练冻结副本的代码/数据恢复；不覆盖历史生产证据",
            })
            ev["_detail"] = f"app 源码 {len(frozen_hashes)} 个文件哈希一致，回退等值"

    async def phase_real(self) -> None:
        """真实后端：显式 --run-real + --budget 才执行；仍只用临时库/目录/端口。"""
        import httpx

        if not self.allow_real:
            self.rec.record("real.backends", "真实后端 3 后端 DAG", NOT_RUN,
                            "未提供 --run-real：本条留待父进程显式执行")
            return
        if self.real_evidence.exists() and not self.force_real:
            self.rec.record("real.backends", "真实后端 3 后端 DAG", BLOCKED,
                            f"已存在真实调用证据 {self.real_evidence}；"
                            "为避免自动重跑副作用，需显式 --force-real 才覆盖")
            return
        from app.security import sandbox

        # 预算按**每次真实 adapter 调用**实时扣减（RealCallGate），不再预留最坏轮次：
        # 未执行的预算不进入 "已调用"，超限的调用被硬拒绝而不是静默超支。
        if self.budget < 1:
            self.rec.record("real.budget", "真实调用预算", BLOCKED,
                            f"声明预算 {self.budget} < 1：无法执行任何真实调用")
            return
        self.rec.record("real.budget", "真实调用预算", PASS,
                        f"声明 {self.budget} 次真实 adapter 调用上限；按每次实际调用计数，"
                        "未执行的预算保留不计入已调用（broker 多轮按真实轮次计数）")

        with self.rec.step("real.load_settings", "加载父进程真实 Settings 并覆盖临时路径") as ev:
            from app.agents.registry import AgentRegistry
            from app.config import load_settings

            # 优先使用父进程**已加载**的 Settings（避免重复读取凭据/环境）；
            # 仅在未提供时按真实配置加载（CLI --run-real 路径）。
            settings = self.real_settings or self.settings or load_settings()
            temp = self.temp / "real-case"
            workspace = self.workspace_dir("real-case")
            (workspace / "input.txt").write_text("REAL_DAG_INPUT", encoding="utf-8")
            (workspace / "dag.txt").write_text("ORIGINAL", encoding="utf-8")
            write_scaffold_test(workspace)
            access_token = "sim-real-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
            token_main = "real-api-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
            update = {
                "database_path": str(temp / "real.db"),
                "thinking_archive_dir": "",
                "transport": "onebot_http",
                "onebot_http_api_url": f"http://127.0.0.1:{self.sim_server.port}",
                "onebot_access_token": access_token,
                "http_listen_port": 0,
                "http_listen_host": "127.0.0.1",
                "file_read_root": "",
                "file_write_root": "",
                "zcode_workspace_root": str(workspace),
                "codex_workdir": str(workspace),
                "codex_allowed_roots": [],
                "deepseek_harness_workdir": str(workspace),
                "console_workspaces": {
                    "roots": [{
                        "path": str(workspace),
                        "modes": ["read", "edit", "exec"],
                        "admin_modes": ["read", "edit", "exec"],
                        "searchable": True,
                        "label": "real-final-verify",
                    }],
                    "backup_dir": str(temp / "backups"),
                    "find": {"max_depth": 4, "max_candidates": 8, "max_seconds": 3.0},
                },
                "console_api": {"tokens": [api_token(
                    "real", token_main, int((settings.qq_allowed_user_ids or [100])[0]),
                    scopes=["task", "team", "workspace"],
                    groups=tuple(settings.qq_allowed_group_ids or (200,)),
                )]},
            }
            real = settings.model_copy(update=update)
            registry = AgentRegistry.from_settings(real)
            missing = []
            for name in ("deepseek", "zcode", "codex"):
                backends = registry.backends.for_agent(name)
                if registry.backends.agent(name) is None or not any(
                    item.descriptor.configured for item in backends
                ):
                    missing.append(name)
            require(not missing, f"真实后端未配置/未启用：{missing}；不启动真实 DAG")
            # 不虚写：DAG/HTTP 用到的三个 Agent 不得落到 mock 后端
            # （否则 fake 调用会被计成真实调用）。
            mock_backends = sorted(
                item.id for name in ("deepseek", "zcode", "codex")
                for item in registry.backends.for_agent(name)
                if item.descriptor.adapter_type == "mock_harness"
            )
            require(not mock_backends,
                    f"真实阶段检测到 mock 后端 {mock_backends}：拒绝把 fake 调用记为真实调用")
            require(real.qq_allowed_user_ids and real.qq_allowed_group_ids,
                    "真实配置缺少 QQ 授权名单，无法安全驱动 QQ 入口")
            ev.update({"settings": self.settings_summary(real), "missing": missing,
                       "sandbox": sandbox.self_check(),
                       "adapter_injection": bool(self.agents),
                       "workspace_contract": self.assert_authorized_search_root(
                           real, user_id=int((real.qq_allowed_user_ids or [100])[0]),
                           group_id=int((real.qq_allowed_group_ids or [200])[0])),
                       "registry_agents": sorted(registry.backends.agents)})
            self.real_settings_overlay = real
            self.real_access_token = access_token
            self.real_token_main = token_main
            self.real_workspace = workspace
            self.real_user = int((real.qq_allowed_user_ids or [100])[0])
            self.real_group = int((real.qq_allowed_group_ids or [200])[0])
            ev["_detail"] = "真实 Settings 已加载，路径/端口/Token 全部覆盖为临时值"
        if not hasattr(self, "real_settings_overlay"):
            return

        real = self.real_settings_overlay
        workspace = self.real_workspace
        from app.main import create_app

        server = await self.start_app_server(create_app(real), "app-real")
        injected = self.install_real_adapters(server.app, workspace)
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}",
                                   trust_env=False, timeout=300.0)
        channel = QQChannel(client, self.sim, real, server.app,
                            access_token=self.real_access_token,
                            group_id=self.real_group, user_id=self.real_user)
        exec_policy = (real.console_commands or {}).get("exec") or {}
        exec_ready = bool(exec_policy.get("enabled", True)) and bool(sandbox.available())
        try:
            async with self.rec.astep("real.http_text", "真实后端 HTTP text 入口") as ev:
                ev["injected_backends"] = injected
                ev["budget_policy"] = "per-call hard gate（未执行不计数，超限硬失败）"
                response = await client.post(
                    "/api/v1/tasks",
                    headers={"Authorization": "Bearer " + self.real_token_main,
                             "Idempotency-Key": "real-" + hashlib.sha256(os.urandom(8)).hexdigest()[:10]},
                    json={"agent": "deepseek", "mode": "text",
                          "prompt": "只返回这个标记，不要解释：REAL_HTTP_OK"},
                )
                require(response.status_code == 202, f"提交失败：{response.status_code} {response.text[:200]}")
                task_id = response.json()["task_id"]

                async def terminal() -> Any:
                    current = await client.get(f"/api/v1/tasks/{task_id}/result",
                                               headers={"Authorization": "Bearer " + self.real_token_main})
                    payload = current.json()
                    return payload if payload.get("status") in TASK_TERMINAL else False

                payload = await poll_until(terminal, timeout=280, interval=0.5,
                                           message="真实 text 任务未收敛")
                require(payload.get("status") == "succeeded",
                        f"真实 text 终态：{payload.get('status')} {payload.get('error')}")
                content = str(payload.get("content") or "")
                require(content, "真实 text 正文为空（失败判定，不缩减为通过）")
                require(not self.gate.exceeded, "真实调用预算已超限（硬门禁触发）")
                ev.update({"task_id": task_id, "status": payload["status"],
                           "content_len": len(content),
                           "marker_present": "REAL_HTTP_OK" in content,
                           "content_first_200": content[:200],
                           "calls_after_http": self.real_used})

            if not exec_ready:
                self.rec.record("real.dag", "真实 3 后端 read→edit→exec DAG", NA,
                                "本机无平台隔离或配置关闭 exec：如实降级，不冒充 3 后端全 DAG")
            else:
                async with self.rec.astep("real.dag", "真实 3 后端 read→edit→exec DAG") as ev:
                    ev["calls_before_dag"] = self.real_used
                    # exec 步骤使用父进程项目解释器 + `-m pytest`（见 scaffold_exec_instruction）；
                    # 预算由 per-call gate 硬门禁，不再预留"3 步 × 2 轮"。
                    dag_ev = await self._run_dag(
                        channel, workspace, phase="real",
                        agents=("deepseek", "zcode", "codex"),
                        instructions={"exec": scaffold_exec_instruction()},
                        pytest_scaffold=True)
                    ev["team_id"] = dag_ev.get("team_id")
                    ev["dag_status"] = dag_ev.get("status")
                    ev["calls_after_dag"] = self.real_used
                    dag_check = next((item for item in reversed(self.rec.checks)
                                      if item.check_id == "dag.real"), None)
                    if dag_check is not None and dag_check.status == SKIP:
                        ev["_skip"] = (f"exec 平台不可用，真实 DAG 降级为 read→edit："
                                       f"{dag_check.detail}")
                    elif dag_check is None or dag_check.status != PASS:
                        require(False, f"真实 DAG 未通过：{getattr(dag_check, 'status', None)} "
                                        f"{getattr(dag_check, 'detail', '')}")
                    else:
                        ev["_detail"] = f"真实 DAG 通过：{dag_ev.get('dag_text')}"
        finally:
            await client.aclose()
            await self.stop_app_server(server)

    # ---------- 收尾 ----------

    def finalize_budget(self) -> None:
        """收尾时把预算硬门禁结果落成独立检查（超限必须失败，不能只看子任务状态）。"""
        if not self.allow_real:
            return
        if self.gate.exceeded:
            self.rec.record(
                "real.budget_hard_gate", "真实调用预算硬门禁", FAIL,
                f"真实 adapter 调用达到/超过声明预算 {self.budget}："
                f"已拒绝后续调用；实际调用 {self.gate.calls} 次",
                {"calls": self.gate.calls, "budget": self.budget,
                 "units": sorted({str(item.get("unit")) for item in self.gate.entries})},
            )
        else:
            self.rec.record(
                "real.budget_hard_gate", "真实调用预算硬门禁", PASS,
                f"实际调用 {self.gate.calls}/{self.budget} 次，未超限；未执行不计数",
                {"calls": self.gate.calls, "budget": self.budget,
                 "units": sorted({str(item.get("unit")) for item in self.gate.entries})},
            )

    def report(self, *, mode: str) -> dict:
        counts = self.rec.counts()
        failed = [c.check_id for c in self.rec.checks if c.status == FAIL]
        blocked = [c.check_id for c in self.rec.checks if c.status == BLOCKED]
        not_run = [c.check_id for c in self.rec.checks if c.status == NOT_RUN]
        pytest_check = next((c for c in self.rec.checks if c.check_id == "suite.pytest"), None)
        pytest_ok = pytest_check is None or pytest_check.status in (PASS, SKIP)
        passed = not failed and not blocked and pytest_ok
        real_calls = self.gate.report()
        # 凭据只在显式 --run-real / allow_real 时才可能被读取；默认 fake 模式不读。
        real_calls["credentials_read"] = bool(self.allow_real)
        return {
            "generated_at": now_text(),
            "mode": mode,
            "workspace": str(ROOT),
            "python": sys.version.split()[0],
            "counts": counts,
            "checks": [
                {"id": c.check_id, "title": c.title, "status": c.status,
                 "detail": c.detail, "evidence": redact_paths(c.evidence)}
                for c in self.rec.checks
            ],
            "a_matrix": {key: {"title": value[0], "regression": list(value[1])}
                         for key, value in A_MATRIX.items()},
            "real_calls": real_calls,
            "verdict": {"passed": passed, "failed": failed, "blocked": blocked,
                        "not_run": not_run},
        }

    def write_report(self, document: dict, *, real: bool) -> Path | None:
        target = self.json_out
        if target is None:
            target = self.real_evidence if real else DEFAULT_EVIDENCE
        if "pytest" in sys.modules and self.json_out is None:
            print("[evidence] 检测到 pytest 进程：不写默认证据路径，如需落盘请显式 --json-out",
                  file=sys.stderr)
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        return target

    def print_summary(self, document: dict, evidence_path: Path | None) -> None:
        counts = document["counts"]
        real = document.get("real_calls") or {}
        print("\n" + "=" * 78)
        print(f"统一最终交付验证 · {document['mode']} · {document['generated_at']}")
        print(f"通过 {counts[PASS]} / 失败 {counts[FAIL]} / 阻塞 {counts[BLOCKED]} / "
              f"跳过 {counts[SKIP]} / 不适用 {counts[NA]} / 待执行 {counts[NOT_RUN]}")
        print(f"真实 adapter 调用 {real.get('calls', 0)} / 预算 "
              f"{real.get('declared_budget', 0)}（预留 {real.get('reserved', 0)}；"
              f"未执行不计；凭据读取={real.get('credentials_read', False)}）")
        for check in document["checks"]:
            if check["status"] in (FAIL, BLOCKED):
                print(f"  ! [{check['status']}] {check['id']} {check['title']} — {check['detail']}")
        scope = "真实隔离验收范围" if document['mode'] == 'real' else "离线范围"
        print(f"结论：{'通过（' + scope + '）' if document['verdict']['passed'] else '未通过/有阻塞'}")
        if evidence_path is not None:
            print(f"证据：{evidence_path}")
        print("父进程接续（真实调用，需项目 venv；budget 是真实 adapter 调用上限，按次硬计数）:")
        print(f'  "{sys.executable}" scripts/verify_final_delivery.py --run-real --budget 30')
        print("=" * 78)


async def run_verification(
    *, settings: Any = None, agents: Mapping[str, Any] | None = None,
    allow_real: bool = False, budget: int | None = None, run_pytest: bool = True,
    json_out: Path | str | None = None, keep_temp: bool = False,
    real_settings: Any = None, real_evidence: Path | str | None = None,
    force_real: bool = False,
) -> int:
    """程序化入口；返回进程退出码（0 通过 / 1 失败 / 3 阻塞）。"""
    verifier = FinalDeliveryVerifier(
        settings=settings, agents=agents, allow_real=allow_real, budget=budget,
        run_pytest=run_pytest,
        json_out=Path(json_out) if json_out else None, keep_temp=keep_temp,
        real_settings=real_settings,
        real_evidence=Path(real_evidence) if real_evidence else None,
    )
    verifier.force_real = bool(force_real)
    verifier.phase_dependencies()
    if not verifier.deps_ok:
        # 缺依赖时连临时服务器都不启动：如实 BLOCKED，退码 3，不伪装成业务失败
        document = verifier.report(mode="real" if allow_real else "offline")
        evidence_path = verifier.write_report(document, real=allow_real)
        verifier.print_summary(document, evidence_path)
        return 3
    # 真实 exec 步骤只给解释器命令名（见 scaffold_exec_instruction）；这里把项目解释器
    # 目录前置到**本验证进程**的 PATH，`async with` 保证所有任务执行结束、异常或取消后恢复原值。
    async with interpreter_on_path():
        async with verifier:
            try:
                verifier.phase_runtime()
                verifier.phase_temp_config()
                verifier.phase_pytest()
                lifecycle = await verifier.phase_http_lifecycle()
                await verifier.phase_restart(lifecycle)
                await verifier.phase_http_dag(lifecycle)
                verifier.phase_recovery(lifecycle)
                if agents and allow_real:
                    await verifier.phase_adapter_dag()
                await verifier.phase_real()
            except Exception as exc:  # noqa: BLE001 - 骨架异常也必须落证据
                verifier.rec.record(
                    "harness.fatal", "验证流程", FAIL, f"{type(exc).__name__}: {exc}",
                    {"traceback": tail_text(traceback.format_exc(), 2000)},
                )
            verifier.finalize_budget()
            document = verifier.report(mode="real" if allow_real else "offline")
            evidence_path = verifier.write_report(document, real=allow_real)
            verifier.print_summary(document, evidence_path)
            counts = document["counts"]
            if counts[FAIL] or counts[BLOCKED]:
                return 3 if counts[BLOCKED] and not counts[FAIL] else 1
            return 0


# 父进程 harness 入口别名：harness_run(...) 与 run_verification(...) 完全等价
harness_run = run_verification


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一最终交付验证（默认离线）")
    parser.add_argument("--run-real", action="store_true",
                        help="显式允许真实模型调用（同时必须给出 --budget）")
    parser.add_argument("--budget", type=int, default=None,
                        help="真实 adapter 调用次数上限（--run-real 时必填，按次硬计数；"
                             "broker 可多轮，建议 30）")
    parser.add_argument("--json-out", default="", help="证据输出路径")
    parser.add_argument("--real-json-out", default="", help="真实调用证据输出路径")
    parser.add_argument("--force-real", action="store_true",
                        help="允许覆盖已存在的真实调用证据（默认拒绝，避免自动重跑副作用）")
    parser.add_argument("--no-pytest", action="store_true", help="跳过完整 pytest（只跑装配检查）")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时目录用于事后审查")
    args = parser.parse_args(argv)
    if args.run_real and not args.budget:
        print("--run-real 必须同时声明 --budget（真实 adapter 调用次数上限）。", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_verification(
            allow_real=args.run_real, budget=args.budget,
            run_pytest=not args.no_pytest,
            json_out=args.json_out or None,
            keep_temp=args.keep_temp,
            real_evidence=args.real_json_out or None,
            force_real=args.force_real,
        ))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
