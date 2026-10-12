"""ExecutionGateway：选择适配器、校验能力、执行 broker 或已验证的原生工具。

- 业务层不再出现 `name == "codex"` 之类分支：能力来自 BackendDescriptor；
- text：直接调用后端，禁用一切文件/命令能力；
- read/edit/exec：默认走公共 broker（统一语义、真实差异与哈希）；只有在
  「适配器声明原生能力 + 隔离可验证 + 部署显式开启 native_tools」时才走原生工具，
  并在执行前后对工作区做有界快照以记录实际变更；
- 后端不支持所请求模式时明确返回 capability_not_supported，绝不偷偷换后端或降级。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from app.agents.prompts import build_system_prompt
from app.backends.broker_loop import (
    OPERATION_FAILED,
    OPERATION_NONE,
    OPERATION_OBSERVED,
    build_broker_messages,
    no_operation_notice,
    observed_any_action,
    run_broker_session,
)
from app.backends.descriptor import ISOLATION_SANDBOX
from app.backends.protocol import ChangeRecord, ExecutionResult, ExecutionSpec
from app.backends.registry import BackendRegistry
from app.commands.options import MODE_TEXT
from app.models import AgentReply, ChatMessage, InvocationOptions
from app.security.redaction import SecretRedactor
from app.workspaces.broker import BrokerBudget, FileBroker
from app.workspaces.models import TaskCancelled
from app.workspaces.changes import file_hash

logger = logging.getLogger("qqbot.backends.gateway")

_NATIVE_SNAPSHOT_LIMIT = 2000

_ERROR_TEXT = {
    "timeout": "调用超时",
    "cancelled": "任务已取消",
    "commit_failed": "文件提交失败（版本冲突或写入错误）",
    "network": "网络错误",
    "parse": "响应解析失败",
    "harness_failed": "本机 Harness 调用失败",
    "harness_approval_required": "本机 Harness 需要人工交互，当前未接入",
    "selection_mismatch": "返回的模型或强度与选择不一致，任务已停止",
    "output_limit": "输出超过大小限制",
    "input_limit": "输入超过大小限制",
    "codex_failed": "Codex 调用失败",
    "not_configured": "后端尚未配置",
    "bad_actions": "模型没有按要求返回结构化动作",
    "broker_denied": "文件代理拒绝执行（权限/越界/预算）",
    "budget_exceeded": "动作预算耗尽",
    "internal": "内部错误（详情见服务端日志）",
    "unknown_backend": "后端不存在",
    "rate_limited": "请求过于频繁",
    "unauthorized": "授权已撤销或未授权",
    "workspace_busy": "目标目录正被其他任务占用",
    "empty": "模型返回空内容",
    "invalid_option": "模型或强度不可用",
    "capability_not_supported": "后端不支持该模式",
}


def error_text(kind: str | None) -> str:
    if not kind:
        return "未知错误"
    if kind.startswith("api_error_"):
        return f"API 错误（HTTP {kind.removeprefix('api_error_')}）"
    return _ERROR_TEXT.get(kind, "未知错误")


class ExecutionGateway:
    def __init__(
        self,
        *,
        registry: BackendRegistry,
        settings,
        redactor: SecretRedactor,
        exec_policy,
        broker_budget: BrokerBudget | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._redactor = redactor
        self._exec_policy = exec_policy
        self._budget = broker_budget or BrokerBudget()

    @property
    def registry(self) -> BackendRegistry:
        return self._registry

    # ---------- 入口 ----------

    async def execute(
        self,
        *,
        spec: ExecutionSpec,
        prompt: str,
        history: Sequence[ChatMessage] = (),
        broker: FileBroker | None = None,
    ) -> ExecutionResult:
        backend = self._registry.get(spec.backend_id)
        if backend is None:
            return self._failure(spec, "unknown_backend", f"未知后端：{spec.backend_id}")
        descriptor = backend.descriptor
        if not descriptor.enabled:
            return self._failure(spec, "not_configured", f"后端 {spec.backend_id} 已禁用。")
        if not descriptor.configured:
            return self._failure(
                spec, "not_configured",
                f"后端 {spec.backend_id} 未配置：{descriptor.unavailable_reason or '缺少配置'}",
            )
        if not descriptor.supports(spec.mode):
            return self._failure(spec, "capability_not_supported", descriptor.capability_error(spec.mode))
        if spec.model and descriptor.models and spec.model not in descriptor.models:
            return self._failure(
                spec, "invalid_option",
                f"模型 {spec.model} 不在后端 {spec.backend_id} 的真实目录中。",
            )
        efforts = descriptor.model_efforts.get(spec.model, ())
        if spec.effort is not None and spec.effort not in efforts:
            return self._failure(
                spec, "invalid_option",
                f"强度 {spec.effort} 不受模型 {spec.model} 支持。",
            )

        started = time.monotonic()
        if spec.mode == MODE_TEXT:
            result = await self._run_text(spec, backend, prompt, history)
        elif self._use_native(spec, backend):
            result = await self._run_native(spec, backend, prompt, history)
        else:
            if broker is None:
                return self._failure(
                    spec, "internal", f"{spec.mode} 模式需要公共文件 broker，但未提供。"
                )
            result = await self._run_broker(spec, backend, prompt, broker)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.task_id = spec.task_id
        result.isolation = descriptor.isolation
        if result.error:
            result.ok = False
            result.status = "failed"
        return result

    def _use_native(self, spec: ExecutionSpec, backend) -> bool:
        descriptor = backend.descriptor
        if not spec.use_native:
            return False
        if spec.mode not in descriptor.native_modes:
            return False
        if descriptor.isolation != ISOLATION_SANDBOX:
            return False
        return bool(backend.options.get("native_tools"))

    # ---------- text ----------

    async def _run_text(
        self, spec: ExecutionSpec, backend, prompt: str, history: Sequence[ChatMessage]
    ) -> ExecutionResult:
        options = InvocationOptions(
            model=spec.model, effort=spec.effort, directory=None, workspace_tools=False,
            mode=MODE_TEXT, backend=spec.backend_id,
        )
        system = spec.system_prompt or build_system_prompt(backend.adapter.display_name)
        messages = [
            ChatMessage(role="system", content=system),
            *history,
            ChatMessage(role="user", content=prompt),
        ]
        size = sum(len(item.content) for item in messages)
        if size > spec.max_input_chars:
            return self._failure(spec, "input_limit", f"输入 {size} 字，超过上限 {spec.max_input_chars}")
        reply = await self._call(backend, messages, options, spec)
        if reply.error:
            # L01：broker 会话之外的直接调用也要带回诊断
            return self._failure(spec, reply.error, self._diagnostic_detail(reply))
        content = (reply.content or "").strip()
        if not content:
            return self._failure(spec, "empty", "模型返回空内容。")
        if len(content) > spec.max_output_chars:
            return self._failure(
                spec, "output_limit", f"输出 {len(content)} 字，超过上限 {spec.max_output_chars}"
            )
        return ExecutionResult(
            ok=True, status="succeeded", content=self._redactor.redact(content),
            agent=spec.agent, backend=spec.backend_id, model=spec.model,
            effort=self._effort_of(spec, reply),
            mode=spec.mode, thinking=(reply.thinking or "").strip(),
            tool_log=list(reply.tool_log or []),
            # text 是纯答复：操作类别是"只读/会话答复"，永远不带文件变更
            operation=OPERATION_OBSERVED,
        )

    # ---------- broker ----------

    async def _run_broker(
        self, spec: ExecutionSpec, backend, prompt: str, broker: FileBroker
    ) -> ExecutionResult:
        options = InvocationOptions(
            model=spec.model, effort=spec.effort, directory=str(spec.workspace),
            workspace_tools=False, mode=spec.mode, backend=spec.backend_id,
        )
        messages = build_broker_messages(
            prompt=prompt, workspace=str(spec.workspace), mode=spec.mode,
            extra_system=spec.system_prompt,
        )

        async def send(items: Sequence[ChatMessage], timeout: float) -> AgentReply:
            return await self._call(backend, list(items), options, spec, timeout=timeout)

        try:
            session = await run_broker_session(
                send=send, messages=messages, broker=broker, budget=self._budget,
                timeout=spec.timeout_seconds, max_input_chars=spec.max_input_chars,
            )
        except TaskCancelled:
            return ExecutionResult(
                ok=False, status="failed", error="cancelled",
                detail="任务已取消，未提交任何变更。",
                agent=spec.agent, backend=spec.backend_id, model=spec.model,
                effort=spec.effort, mode=spec.mode, changes=self._commit(broker),
            )
        try:
            changes = self._commit(broker)
        except TaskCancelled:
            return ExecutionResult(
                ok=False, status="failed", error="cancelled",
                detail="任务在提交变更前被取消。",
                agent=spec.agent, backend=spec.backend_id, model=spec.model,
                effort=spec.effort, mode=spec.mode, changes=[],
            )
        changes = changes + self._command_changes(broker)
        applied = [change for change in changes if change.status == "applied"]
        failed = [change for change in changes if change.status != "applied"]
        # F07：有关键提交失败 → 任务必须失败，且保留全部变更/备份/错误
        if failed:
            detail = "；".join(
                f"{Path(change.path).name} {change.status}：{change.error}" for change in failed[:5]
            )
            return ExecutionResult(
                input_fingerprints=self._fingerprints(broker),
                ok=False, status="failed", error="commit_failed",
                detail=f"{len(failed)} 个文件未写入（{detail}）",
                agent=spec.agent, backend=spec.backend_id, model=spec.model,
                effort=spec.effort, mode=spec.mode, thinking=session.thinking,
                tool_log=list(session.tool_log), changes=changes,
                partial=bool(applied),
            )
        if not session.ok:
            return ExecutionResult(
                input_fingerprints=self._fingerprints(broker),
                ok=False, status="failed", error=session.error, detail=session.detail,
                agent=spec.agent, backend=spec.backend_id, model=spec.model,
                effort=spec.effort, mode=spec.mode, thinking=session.thinking,
                tool_log=list(session.tool_log), changes=changes,
                partial=bool(applied),
                operation=OPERATION_FAILED, action_counts=dict(session.action_counts),
            )
        content = session.content
        if len(content) > spec.max_output_chars:
            failure = self._failure(
                spec, "output_limit", f"输出 {len(content)} 字，超过上限 {spec.max_output_chars}"
            )
            failure.changes = changes          # 超限也要保留已发生的变更
            failure.partial = bool(applied)
            failure.operation = OPERATION_FAILED
            failure.action_counts = dict(session.action_counts)
            return failure
        # V01：操作结果由动作/提交/命令回执定义 —— 而不是"模型给出了 final"。
        operation = session.operation_kind(mode=spec.mode, commits=changes)
        tool_log = list(session.tool_log)
        status = "succeeded"
        if operation == OPERATION_NONE:
            # 会话正常结束但模式目标未达成（R62：读过文件不等于改过文件）：
            # 不得显示为"已修改"。这不是系统失败（模型可能确实无需变更），
            # 因此保持 ok=True，但状态与操作记录明确区分"仅答复/仅观察"。
            status = "no_change"
            notice = no_operation_notice(
                mode=spec.mode, kind=operation,
                observed=observed_any_action(session.action_counts),
            )
            if notice:
                tool_log.append(f"broker:operation {operation} - {notice}")
        return ExecutionResult(
            ok=True, status=status, content=self._redactor.redact(content),
            agent=spec.agent, backend=spec.backend_id, model=spec.model,
            # broker 循环里适配器回填的强度由 broker_loop 记录在 session 上（见 BrokerSessionResult.effort）
            effort=self._effort_of(spec, session),
            mode=spec.mode, thinking=session.thinking, tool_log=tool_log,
            changes=changes, input_fingerprints=self._fingerprints(broker),
            operation=operation, action_counts=dict(session.action_counts),
        )

    @staticmethod
    def _fingerprints(broker: FileBroker | None) -> dict[str, str]:
        return broker.input_fingerprints() if broker is not None else {}

    def _command_changes(self, broker: FileBroker | None) -> list[ChangeRecord]:
        """exec 命令直接产生的改动：作为 applied 变更一并记录（可审计、可判漂移）。"""
        if broker is None:
            return []
        return [
            ChangeRecord(
                path=str(spec_workspace / row["path"]) if spec_workspace else row["path"],
                kind=row["kind"], status="applied",
                before_hash=row.get("before_hash", ""), after_hash=row.get("after_hash", ""),
                error=f"由命令产生：{row.get('command', '')}"[:200],
            )
            for row in broker.command_changes()
            for spec_workspace in (broker._root,)  # noqa: SLF001 - 仅用于拼绝对路径
        ]

    def _commit(self, broker: FileBroker) -> list[ChangeRecord]:
        records: list[ChangeRecord] = []
        for change in broker.commit():
            records.append(ChangeRecord(
                path=str(change.path), kind=change.kind, status=change.status,
                before_hash=change.before_hash, after_hash=change.after_hash,
                backup=change.backup, bytes=change.bytes_written, error=change.error,
            ))
        return records

    # ---------- native（仅显式开启且隔离可验证时） ----------

    async def _run_native(
        self, spec: ExecutionSpec, backend, prompt: str, history: Sequence[ChatMessage]
    ) -> ExecutionResult:
        workspace = spec.workspace
        if workspace is None:
            return self._failure(spec, "internal", "原生模式缺少工作目录。")
        before = _snapshot(workspace)
        options = InvocationOptions(
            model=spec.model, effort=spec.effort, directory=str(workspace),
            workspace_tools=True, mode=spec.mode, backend=spec.backend_id,
        )
        messages = [
            ChatMessage(
                role="system",
                content=build_system_prompt(
                    backend.adapter.display_name, file_task=True, mode=spec.mode
                ),
            ),
            *history,
            ChatMessage(role="user", content=prompt),
        ]
        reply = await self._call(backend, messages, options, spec)
        after = _snapshot(workspace)
        changes = _diff_snapshots(before, after)
        if reply.error:
            # L01：native/text 路径同样带上适配器的失败诊断（已脱敏、有界）
            result = self._failure(spec, reply.error, self._diagnostic_detail(reply))
            result.changes = changes
            result.partial = bool(changes)
            return result
        content = (reply.content or "").strip()
        if not content:
            return self._failure(spec, "empty", "模型返回空内容。")
        # V01：原生路径同样按"实际发生的操作"分类：落盘变更 → changed；
        # exec 有命令回执 → executed；否则（read）→ observed。
        operation = _native_operation(spec.mode, changes, reply.tool_log)
        tool_log = list(reply.tool_log or [])
        status = "succeeded"
        if operation == OPERATION_NONE:
            status = "no_change"
            notice = no_operation_notice(
                mode=spec.mode, kind=operation,
                observed=bool(tool_log),
            )
            if notice:
                tool_log.append(f"native:operation {operation} - {notice}")
        return ExecutionResult(
            ok=True, status=status, content=self._redactor.redact(content),
            agent=spec.agent, backend=spec.backend_id, model=spec.model,
            effort=self._effort_of(spec, reply),
            mode=spec.mode, thinking=(reply.thinking or "").strip(),
            tool_log=tool_log, changes=changes,
            partial=any(change.status != "applied" for change in changes),
            operation=operation,
        )

    # ---------- 调用封装 ----------

    async def _call(
        self, backend, messages: list[ChatMessage], options: InvocationOptions,
        spec: ExecutionSpec, *, timeout: float | None = None,
    ) -> AgentReply:
        limit = timeout if timeout is not None else spec.timeout_seconds
        try:
            return await asyncio.wait_for(
                backend.adapter.chat_with_options(messages, timeout=limit, options=options),
                timeout=limit,
            )
        except asyncio.TimeoutError:
            return AgentReply(error="timeout")
        except asyncio.CancelledError:
            raise
        except ValueError:
            logger.exception("后端 %s 参数校验失败", spec.backend_id)
            return AgentReply(error="invalid_option")
        except Exception:
            logger.exception("后端 %s 调用异常", spec.backend_id)
            return AgentReply(error="internal")

    @staticmethod
    def _effort_of(spec: ExecutionSpec, reply=None) -> str | None:
        """实际使用的强度：显式配置优先，否则用适配器回填的 resolved_effort。"""
        if spec.effort:
            return spec.effort
        resolved = getattr(reply, "resolved_effort", None) if reply is not None else None
        return str(resolved) if resolved else None

    def _diagnostic_detail(self, reply) -> str:
        """统一错误文案 + 适配器诊断（已脱敏、有界）。L01：不让用户只看到错误类别。"""
        detail = error_text(getattr(reply, "error", None))
        diagnostic = self._redactor.redact(str(getattr(reply, "diagnostic", "") or "").strip())
        if diagnostic:
            return f"{detail}：{diagnostic}"[:800]
        return detail

    def _failure(self, spec: ExecutionSpec, error: str, detail: str) -> ExecutionResult:
        return ExecutionResult(
            ok=False, status="failed", error=error, detail=detail,
            agent=spec.agent, backend=spec.backend_id, model=spec.model,
            effort=spec.effort, mode=spec.mode, task_id=spec.task_id,
        )


# ---------- 有界工作区快照（仅原生模式使用） ----------


def _native_operation(mode: str, changes: Sequence[ChangeRecord], tool_log: Sequence[str]) -> str:
    """原生路径的操作类别：只看实际落盘变更与工具回执。"""
    from app.backends.broker_loop import (
        OPERATION_CHANGED,
        OPERATION_EXECUTED,
        OPERATION_NONE,
        OPERATION_OBSERVED,
    )

    if any(change.status == "applied" for change in changes):
        return OPERATION_CHANGED
    if mode == "exec":
        executed = [line for line in tool_log if "命令" in str(line) or "exit=" in str(line)]
        return OPERATION_EXECUTED if executed else OPERATION_NONE
    if mode == "edit":
        # 明确要求修改却没有任何落盘变更 → 不能算"已修改"
        return OPERATION_NONE
    if mode == "read":
        return OPERATION_OBSERVED if tool_log else OPERATION_NONE
    return OPERATION_OBSERVED


def _snapshot(root: Path, *, limit: int = _NATIVE_SNAPSHOT_LIMIT) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    try:
        stack = [root]
        while stack and len(snapshot) < limit:
            current = stack.pop()
            for entry in sorted(current.iterdir(), key=lambda item: item.name):
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in {".git", "node_modules", "__pycache__", ".venv"}:
                        continue
                    stack.append(entry)
                elif entry.is_file() and not entry.name.startswith(".env"):
                    try:
                        snapshot[str(entry.relative_to(root))] = file_hash(entry)
                    except OSError:
                        continue
    except OSError:
        return snapshot
    return snapshot


def _diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[ChangeRecord]:
    records: list[ChangeRecord] = []
    for path, digest in sorted(after.items()):
        if path not in before:
            records.append(ChangeRecord(
                path=path, kind="create", status="applied", after_hash=digest,
            ))
        elif before[path] != digest:
            records.append(ChangeRecord(
                path=path, kind="modify", status="applied",
                before_hash=before[path], after_hash=digest,
            ))
    for path, digest in sorted(before.items()):
        if path not in after:
            records.append(ChangeRecord(
                path=path, kind="delete", status="applied", before_hash=digest,
            ))
    return records
