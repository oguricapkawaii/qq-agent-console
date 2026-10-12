"""TaskService：单任务、广播子任务与 team 步骤的唯一执行入口。

职责（统一架构第 2、4、5 节）：
- 统一授权（群/用户/目录 ACL/模式/后端能力/全局策略的交集）；
- 模式校验（不从正文推断 edit/exec）、限流、目录锁、调用、取消；
- 变更记录（哈希/备份/部分失败）、任务与事件持久化、归档；
- 实时复查授权：开始执行前、每次 broker 动作、提交变更前；
- 失败/取消如实记录已有副作用，不承诺整体原子回滚。

team 步骤通过同一个入口执行（`use_chat_context=False`），因此不会读取或写入普通
聊天历史，也不会绕过任何权限检查。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.backends.gateway import ExecutionGateway, error_text as gateway_error_text
from app.backends.protocol import ChangeRecord, ExecutionResult, ExecutionSpec, TaskRequest
from app.backends.registry import BackendRegistry
from app.commands.options import MODE_TEXT
from app.models import ChatMessage
from app.security.acl import AclService, mode_allows
from app.security.confirmations import InMemoryConfirmationStore
from app.security.rate_limit import SlidingWindowRateLimiter
from app.security.redaction import SecretRedactor
from app.storage.context_store import ASKER_PREFIX, speaker_prefix, thread_key
from app.workspaces.broker import BrokerBudget, FileBroker
from app.workspaces.coordinator import WorkspaceBusy, WorkspaceCoordinator
from app.workspaces.models import TaskCancelled
from app.workspaces.service import WorkspaceService

from app.tasks.store import TERMINAL_STATUSES as TERMINAL_TASK_STATUSES

logger = logging.getLogger("qqbot.tasks.service")

RATE_LIMITED_TEXT = "⏳ 请求过于频繁，请稍后再试。"
NOT_FOUND_TEXT = "⛔ 任务不存在或不属于你。"

# V01：会话正常结束但没有任何实际操作时的任务状态（既不是失败，也不是"已修改"）。
STATUS_NO_CHANGE = "no_change"
# 终态判定统一引用 store，避免"取消/撤权"路径漏掉新状态。
TERMINAL_STATUS_SET = frozenset(TERMINAL_TASK_STATUSES)


@dataclass(slots=True)
class TaskOutcome:
    """一次任务的公开结果。"""

    task_id: str
    ok: bool
    status: str
    agent: str = ""
    display: str = ""
    backend: str = ""
    content: str = ""
    error: str | None = None
    detail: str = ""
    duration_ms: int = 0
    model: str = ""
    effort: str | None = None
    mode: str = MODE_TEXT
    thinking: str = ""
    tool_log: list[str] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    input_fingerprints: dict[str, str] = field(default_factory=dict)
    partial: bool = False
    # V01：操作结果类别（changed/observed/executed/none/failed）与逐动作回执计数
    operation: str = ""
    action_counts: dict[str, int] = field(default_factory=dict)

    @property
    def error_text(self) -> str:
        return gateway_error_text(self.error)

    @property
    def changed_files(self) -> list[ChangeRecord]:
        return [change for change in self.changes if change.status == "applied"]

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "ok": self.ok, "status": self.status,
            "agent": self.agent, "backend": self.backend, "model": self.model,
            "effort": self.effort, "mode": self.mode, "error": self.error,
            "detail": self.detail, "duration_ms": self.duration_ms,
            "operation": self.operation, "action_counts": dict(self.action_counts),
            "changes": [change.as_dict() for change in self.changes],
            "partial": self.partial,
        }


class TaskService:
    def __init__(
        self,
        *,
        settings,
        registry: BackendRegistry,
        gateway: ExecutionGateway,
        acl: AclService,
        workspaces: WorkspaceService,
        coordinator: WorkspaceCoordinator,
        store,
        confirmations: InMemoryConfirmationStore,
        rate_limiter: SlidingWindowRateLimiter,
        redactor: SecretRedactor,
        context_store=None,
        broker_budget: BrokerBudget | None = None,
        exec_policy=None,
        backup_root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._gateway = gateway
        self._acl = acl
        self._workspaces = workspaces
        self._coordinator = coordinator
        self._store = store
        self._confirmations = confirmations
        self._rate_limiter = rate_limiter
        self._redactor = redactor
        self._context = context_store
        self._budget = broker_budget or BrokerBudget()
        self._exec_policy = exec_policy
        self._backup_root = backup_root
        commands_config = getattr(settings, "console_commands", None) or {}
        broker_config = commands_config.get("broker") if isinstance(commands_config, dict) else None
        self._broker_deadline = float(
            (broker_config or {}).get("max_task_seconds") or 300.0
        )
        self._running: dict[str, asyncio.Task] = {}

    # ---------- 查询 ----------

    @property
    def registry(self) -> BackendRegistry:
        return self._registry

    @property
    def workspaces(self) -> WorkspaceService:
        return self._workspaces

    def own_task(self, task_id: str, *, group_id: int, user_id: int) -> dict | None:
        record = self._store.get_task(task_id)
        if record is None:
            return None
        if record["group_id"] != group_id or record["user_id"] != user_id:
            # 不存在与不归本人在文案上不可区分，避免侧信道
            return None
        return record

    # ---------- 冻结选择 ----------

    def resolve_selection(
        self, *, agent_name: str, group_id: int, user_id: int,
        model: str | None, effort: str | None, effort_set: bool,
        backend_id: str | None = None,
    ) -> tuple[tuple[str, str, str | None] | None, str]:
        """冻结 backend/model/effort：显式参数 > 保存选择 > Agent 默认。

        返回 ``((backend_id, model, effort), "")`` 或 ``(None, 拒绝原因)``。
        """
        # 后端选择优先级：本次显式 --backend > 你保存的后端 > Agent 默认后端。
        # 必须在 resolve() 之前读出保存值，否则默认后端会先被选中。
        saved = None
        if self._context is not None:
            saved = self._context.preferences(group_id=group_id, user_id=user_id, agent=agent_name)
        if backend_id is None and saved is not None and saved.backend:
            backend_id = saved.backend
        backend, error = self._registry.resolve(agent_name, backend_id)
        if backend is None:
            return None, error
        descriptor = backend.descriptor
        if not descriptor.enabled:
            return None, f"Agent {agent_name} 的后端 {backend.id} 已禁用。"
        agent_runtime = self._registry.agent(agent_name)
        default_model = (
            (saved.model if saved is not None else None)
            or (agent_runtime.default_model if agent_runtime else "")
            or descriptor.default_model
            or next(iter(descriptor.models), "")
        )
        chosen_model = model or default_model
        if not chosen_model:
            if not descriptor.configured:
                return None, (
                    f"[{descriptor.display_name}] 尚未配置："
                    f"{descriptor.unavailable_reason or '缺少配置'}。"
                )
            return None, f"Agent {agent_name} 没有可用模型；请用 /agent models {agent_name} 查看。"
        if descriptor.models and chosen_model not in descriptor.models:
            return None, (
                f"模型 {chosen_model} 不在后端 {backend.id} 的真实目录中；"
                f"请用 /agent models {agent_name} 查看。"
            )
        if effort_set:
            chosen_effort = effort
        elif model is not None and model != (saved.model if saved is not None else default_model):
            chosen_effort = None
        else:
            chosen_effort = (
                (saved.effort if saved is not None else None)
                or (agent_runtime.default_effort if agent_runtime else None)
            )
        efforts = descriptor.model_efforts.get(chosen_model, ())
        if chosen_effort is not None and chosen_effort not in efforts:
            return None, (
                f"强度 {chosen_effort} 不受模型 {chosen_model} 支持；"
                f"可选：{', '.join(efforts) or '（该模型无强度选项）'}"
            )
        return (backend.id, chosen_model, chosen_effort), ""

    def build_spec(
        self, *, request: TaskRequest, workspace: Path | None, mode: str,
        grant_label: str = "", system_prompt: str = "",
        deadline_seconds: float = 0.0,
    ) -> tuple[ExecutionSpec | None, str]:
        choice, error = self.resolve_selection(
            agent_name=request.agent, group_id=request.group_id, user_id=request.user_id,
            model=request.model, effort=request.effort, effort_set=request.effort_set,
            backend_id=request.backend,
        )
        if choice is None:
            return None, error
        backend_id, chosen_model, chosen_effort = choice
        backend = self._registry.get(backend_id)
        descriptor = backend.descriptor if backend else None
        if descriptor is not None and not descriptor.supports(mode):
            return None, descriptor.capability_error(mode)
        if mode != MODE_TEXT:
            if workspace is None:
                return None, f"{mode} 模式需要目录。"
            decision = self._acl.authorize_path(
                user_id=request.user_id, group_id=request.group_id, path=workspace,
                mode=mode,
                backend_modes=descriptor.effective_modes() if descriptor else None,
            )
            if not decision.allowed:
                return None, decision.reason
            grant_label = decision.grant.label if decision.grant else grant_label
        use_native = mode != MODE_TEXT and descriptor is not None and (
            mode in descriptor.native_modes and descriptor.isolation == "sandbox"
            and bool(backend.options.get("native_tools"))
        )
        if deadline_seconds <= 0 and mode != MODE_TEXT:
            # 多轮 broker 会话必须有整体期限，避免"每轮各自超时"导致总时长无界
            deadline_seconds = self._broker_deadline
        final = ExecutionSpec(
            task_id=request.task_id, backend_id=backend_id, agent=request.agent,
            model=chosen_model, effort=chosen_effort, mode=mode, workspace=workspace,
            grant_label=grant_label,
            timeout_seconds=self._settings.request_timeout_seconds,
            deadline_seconds=deadline_seconds,
            max_input_chars=self._settings.team_max_input_chars,
            max_output_chars=self._settings.team_max_output_chars,
            use_broker=mode != MODE_TEXT and not use_native,
            use_native=use_native,
            entry=request.entry, team_id=request.team_id, step_id=request.step_id,
            system_prompt=system_prompt,
        )
        return final, ""

    # ---------- 执行 ----------

    async def run(
        self, request: TaskRequest, *, spec: ExecutionSpec, history: Sequence[ChatMessage] = ()
    ) -> TaskOutcome:
        """执行一次任务：授权/限流/目录锁/调用/记录全部在这里收敛。

        预登记行（带幂等键）**绝不能永久停留在 queued/running**：登记之后出现的非预期
        异常统一收敛为 ``failed`` 终态（只改仍活动的行，取消/中断等终态优先），并只记录
        脱敏诊断（异常类型，不含正文）。异常本身继续向上抛出：不吞、不自动重试。
        """
        task_id = request.task_id or self._store.new_id()
        if spec.task_id != task_id:
            # 统一任务 ID：spec 与记录、运行中索引、broker 变更记录必须一致
            spec = dataclasses.replace(spec, task_id=task_id)
        try:
            return await self._run_task(request, spec=spec, history=history, task_id=task_id)
        except asyncio.CancelledError:
            # 取消由 cancel()/cancel_all_for() 落终态，这里不把取消改写成失败
            raise
        except Exception as exc:
            if self._store.update_status_if_active(task_id, "failed", error="internal_error"):
                self._store.add_event(task_id, "error", f"unexpected:{type(exc).__name__}"[:200])
            raise

    async def _run_task(
        self, request: TaskRequest, *, spec: ExecutionSpec,
        history: Sequence[ChatMessage], task_id: str,
    ) -> TaskOutcome:
        if not self._rate_limiter.allow(request.user_id):
            return self._reject(
                request=request, spec=spec, task_id=task_id, error="rate_limited",
                detail=RATE_LIMITED_TEXT,
            )
        # 开始执行前复查实时授权
        if not self._acl.authorize_binding(
            user_id=request.user_id, group_id=request.group_id, mode=spec.mode
        ).allowed:
            return self._reject(
                request=request, spec=spec, task_id=task_id, error="unauthorized",
                detail="你的授权已被撤销。",
            )
        paths = (spec.workspace,) if spec.workspace is not None else ()
        broker: FileBroker | None = None
        if spec.use_broker:
            broker = self._make_broker(request=request, spec=spec)
        display = self._display_name(request.agent)
        # 先落库再执行：/task status、/task cancel 与撤权取消都能找到正在运行的任务
        self._ensure_record(request=request, spec=spec, task_id=task_id, status="running")
        # F05：排队/等锁阶段就登记取消句柄，cancel 才能立刻中止等待者
        current = asyncio.current_task()
        if current is not None:
            self._running[task_id] = current
        try:
            result = await self._run_locked(
                request=request, spec=spec, broker=broker, history=history,
                paths=paths, task_id=task_id,
            )
        except WorkspaceBusy as exc:
            return self._reject(
                request=request, spec=spec, task_id=task_id, error="workspace_busy",
                detail=exc.message,
            )
        except TaskCancelled as exc:
            # 排队期间已被取消/中断：立刻收敛，不调用模型、不写文件、不记历史
            record = self._store.get_task(task_id)
            status = record["status"] if record else "cancelled"
            return TaskOutcome(
                task_id=task_id, ok=False, status=status, agent=request.agent,
                display=display, backend=spec.backend_id, mode=spec.mode,
                error=status, detail=str(exc),
            )
        except asyncio.CancelledError:
            # 只有"服务自身取消且已落终态"才收敛为结果；其余（如进程关闭）继续向上传播
            record = self._store.get_task(task_id)
            if record is not None and record["status"] in TERMINAL_TASK_STATUSES:
                return TaskOutcome(
                    task_id=task_id, ok=False, status=record["status"], agent=request.agent,
                    display=display, backend=spec.backend_id, mode=spec.mode,
                    error=record["error"] or "cancelled",
                    detail="任务已取消：未提交任何新的模型调用或文件写入。",
                )
            raise
        finally:
            self._running.pop(task_id, None)
            if broker is not None:
                # 释放 broker 持有的安全根 fd（长进程里不会累积）
                broker.close()
        return self._finalize(request=request, spec=spec, result=result, task_id=task_id)

    async def _run_locked(
        self, *, request: TaskRequest, spec: ExecutionSpec, broker: FileBroker | None,
        history, paths, task_id: str,
    ) -> ExecutionResult:
        """获取目录锁并在（可选的）总期限内执行；锁内复查终态与授权。"""
        deadline = spec.deadline_seconds if spec.deadline_seconds > 0 else None
        lock_timeout = None
        if deadline is not None:
            lock_timeout = min(deadline, self._settings.workspace_lock_timeout_seconds)
        started = time.monotonic()
        async with self._coordinator.lock(
            paths, label=f"{request.entry}:{request.agent}", task_id=task_id,
            timeout=lock_timeout,
        ):
            self._assert_not_terminal(task_id)
            if broker is not None:
                self._assert_still_authorized(request, spec)
            remaining = None
            if deadline is not None:
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    return self._timeout_result(spec, broker)
            try:
                if remaining is None:
                    return await self._execute_locked(
                        request=request, spec=spec, broker=broker, history=history
                    )
                return await asyncio.wait_for(
                    self._execute_locked(
                        request=request, spec=spec, broker=broker, history=history
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return self._timeout_result(spec, broker)

    def _timeout_result(self, spec: ExecutionSpec, broker: FileBroker | None) -> ExecutionResult:
        """F14：单步/任务期限到点 → 如实失败，并保留已产生的副作用记录。"""
        changes = broker.commit() if broker is not None else []
        records = [
            ChangeRecord(
                path=str(change.path), kind=change.kind, status=change.status,
                before_hash=change.before_hash, after_hash=change.after_hash,
                backup=change.backup, bytes=change.bytes_written, error=change.error,
            )
            for change in changes
        ]
        return ExecutionResult(
            ok=False, status="failed", error="timeout",
            detail=f"任务在 {spec.deadline_seconds:.0f} 秒期限内未完成，已停止。",
            agent=spec.agent, backend=spec.backend_id, model=spec.model,
            effort=spec.effort, mode=spec.mode, changes=records,
            partial=any(record.status == "applied" for record in records),
        )

    def _assert_not_terminal(self, task_id: str) -> None:
        record = self._store.get_task(task_id)
        if record is not None and record["status"] in TERMINAL_TASK_STATUSES:
            raise TaskCancelled(f"任务已是 {record['status']}，不再执行。")

    def _reject(
        self, *, request: TaskRequest, spec: ExecutionSpec, task_id: str,
        error: str, detail: str,
    ) -> TaskOutcome:
        """在真正调用模型之前就失败：同样落库，便于 /task status 追溯。"""
        self._ensure_record(request=request, spec=spec, task_id=task_id, status="running")
        self._store.update_status_if_active(task_id, "failed", error=error)
        self._store.add_event(task_id, "rejected", f"{error}:{detail}"[:200])
        logger.info("task=%s rejected error=%s", task_id, error)
        return TaskOutcome(
            task_id=task_id, ok=False, status="failed", agent=request.agent,
            display=self._display_name(request.agent), backend=spec.backend_id,
            error=error, detail=detail, mode=spec.mode,
        )

    async def _execute_locked(
        self, *, request: TaskRequest, spec: ExecutionSpec, broker: FileBroker | None,
        history: Sequence[ChatMessage],
    ) -> ExecutionResult:
        return await self._gateway.execute(
            spec=spec, prompt=request.prompt,
            history=history if request.use_chat_context else (),
            broker=broker,
        )

    def _ensure_record(
        self, *, request: TaskRequest, spec: ExecutionSpec, task_id: str, status: str
    ) -> str:
        """确保任务记录存在（存在则只补充冻结字段），返回 task_id。"""
        existing = self._store.get_task(task_id)
        if existing is not None:
            self._store.update_task(
                task_id, status=status if existing["status"] not in TERMINAL_TASK_STATUSES
                else None,
                backend=spec.backend_id, model=spec.model, effort=spec.effort,
                mode=spec.mode,
                workspace=str(spec.workspace) if spec.workspace else None,
            )
            return task_id
        return self._store.create_task(
            entry=request.entry, group_id=request.group_id, user_id=request.user_id,
            agent=request.agent, prompt=request.prompt, mode=spec.mode,
            workspace=str(spec.workspace) if spec.workspace else None,
            backend=spec.backend_id, model=spec.model, effort=spec.effort,
            status=status, team_id=request.team_id or None,
            step_id=request.step_id or None, round_no=request.round_no or None,
            idempotency_key=request.idempotency_key, task_id=task_id,
        )

    def _make_broker(self, *, request: TaskRequest, spec: ExecutionSpec) -> FileBroker:
        return FileBroker(
            acl=self._acl, root=spec.workspace, group_id=request.group_id,
            user_id=request.user_id, task_id=spec.task_id, mode=spec.mode,
            budget=self._budget, exec_policy=self._exec_policy,
            backup_root=self._backup_root,
            guard=lambda: self._assert_not_terminal(spec.task_id),
        )

    def _assert_still_authorized(self, request: TaskRequest, spec: ExecutionSpec) -> None:
        if spec.workspace is None:
            return
        decision = self._acl.authorize_path(
            user_id=request.user_id, group_id=request.group_id,
            path=spec.workspace, mode=spec.mode,
        )
        if not decision.allowed:
            raise WorkspaceBusy(decision.reason)

    # ---------- 记录与归档 ----------

    def _finalize(
        self, *, request: TaskRequest, spec: ExecutionSpec, result: ExecutionResult, task_id: str
    ) -> TaskOutcome:
        record_id = self._ensure_record(
            request=request, spec=spec, task_id=task_id, status="running"
        )
        # V01：任务状态由"操作结果"决定 —— 失败照失败，"会话完成但无操作"记为 no_change，
        # 只有真的完成（或纯文本答复）才是 succeeded。
        status = self.task_status_for(result)
        current = self._store.get_task(record_id)
        # 晚到结果不得覆盖终态：任务在等待目录锁或执行期间可能已被取消/中断/撤权取消
        already_terminal = (
            current is not None and current["status"] in TERMINAL_TASK_STATUSES
        )
        action_counts = self._store.json_dump(dict(result.action_counts or {}))
        if already_terminal and current["status"] != status:
            self._store.update_task(
                record_id, error=result.error or current.get("error") or "",
                duration_ms=result.duration_ms, changed_files=len(result.changes),
                result_summary=self._summary(result),
                result_text=(result.content or "")[: self._settings.team_max_output_chars],
                operation=result.operation or "",
                action_counts=action_counts,
            )
            self._store.add_event(
                record_id, "late_result_discarded", f"kept={current['status']} late={status}"
            )
            logger.info(
                "task=%s 晚到结果被丢弃：已是 %s，不覆盖为 %s",
                record_id, current["status"], status,
            )
            status = current["status"]
        else:
            self._store.update_task(
                record_id, status=status, error=result.error or "",
                duration_ms=result.duration_ms, changed_files=len(result.changes),
                result_hash=result.changes[-1].after_hash if result.changes else "",
                result_summary=self._summary(result),
                result_text=(result.content or "")[: self._settings.team_max_output_chars],
                operation=result.operation or "",
                action_counts=action_counts,
            )
            self._store.add_event(record_id, "finished", f"status={status}")
        if result.changes:
            self._store.record_changes(
                task_id=record_id, changes=[change.as_dict() for change in result.changes],
                team_id=request.team_id or None, step_id=request.step_id or None,
                round_no=request.round_no or None,
            )
        # 晚到结果/已取消任务不得写入普通聊天历史（F05）
        if (
            self._context is not None and request.use_chat_context and result.ok
            and not already_terminal
        ):
            self._remember(request, spec, result.content)
        self._archive(request=request, spec=spec, result=result, task_id=record_id)
        outcome = TaskOutcome(
            task_id=record_id, ok=result.ok and not already_terminal, status=status,
            agent=request.agent,
            display=self._display_name(request.agent), backend=spec.backend_id,
            content=result.content, error=result.error, detail=result.detail,
            duration_ms=result.duration_ms, model=spec.model,
            # L04：优先用适配器实际使用的强度（后端模式化默认），否则才是 spec 里的声明
            effort=result.effort or spec.effort,
            mode=spec.mode, thinking=result.thinking, tool_log=list(result.tool_log),
            changes=list(result.changes), partial=result.partial,
            input_fingerprints=dict(result.input_fingerprints),
            operation=result.operation, action_counts=dict(result.action_counts),
        )
        logger.info(
            "task=%s entry=%s agent=%s backend=%s mode=%s status=%s duration_ms=%d changes=%d",
            record_id, request.entry, request.agent, spec.backend_id, spec.mode,
            status, result.duration_ms, len(result.changes),
        )
        return outcome

    @staticmethod
    def task_status_for(result: ExecutionResult) -> str:
        """任务状态 = f(操作结果)，不看模型自述。

        - 失败 → failed；
        - 成功且没有任何实际操作 → no_change（"会话完成/无需变更"，不是失败，
          也不是"已修改"）；
        - 其余成功 → succeeded。
        """
        if not result.ok:
            return "failed"
        if result.operation == "none" or result.status == STATUS_NO_CHANGE:
            return STATUS_NO_CHANGE
        return "succeeded"

    @staticmethod
    def _summary(result: ExecutionResult) -> str:
        text = (result.content or "").strip().replace("\n", " ")
        return text[:200]

    def _display_name(self, agent_name: str) -> str:
        agent = self._registry.agent(agent_name)
        if agent is not None and agent.display_name:
            return agent.display_name
        backend, _ = self._registry.resolve(agent_name)
        return backend.descriptor.display_name if backend else agent_name

    def _remember(self, request: TaskRequest, spec: ExecutionSpec, content: str) -> None:
        if self._context is None:
            return
        shared = bool(self._settings.shared_agent_context)
        thread = thread_key(request.agent, shared=shared)
        asked = (
            f"{ASKER_PREFIX}[{request.user_id}]{request.prompt}" if shared else request.prompt
        )
        stored = (
            f"{speaker_prefix(self._display_name(request.agent))}\n{content}"
            if shared else content
        )
        self._context.append(
            group_id=request.group_id, user_id=request.user_id, agent=thread,
            role="user", content=asked,
        )
        self._context.append(
            group_id=request.group_id, user_id=request.user_id, agent=thread,
            role="assistant", content=stored,
        )
        self._context.trim(
            group_id=request.group_id, user_id=request.user_id, agent=thread,
            max_turns=self._settings.max_context_turns,
        )

    def history_for(self, request: TaskRequest) -> list[ChatMessage]:
        """普通聊天上下文；team 步骤（use_chat_context=False）永远拿到空历史。"""
        if self._context is None or not request.use_chat_context:
            return []
        thread = thread_key(request.agent, shared=bool(self._settings.shared_agent_context))
        return self._context.recent(
            group_id=request.group_id, user_id=request.user_id, agent=thread,
            max_turns=self._settings.max_context_turns,
        )

    # ---------- 取消 ----------

    async def cancel(self, *, task_id: str, group_id: int, user_id: int) -> str:
        record = self.own_task(task_id, group_id=group_id, user_id=user_id)
        if record is None:
            return NOT_FOUND_TEXT
        if record["status"] in TERMINAL_STATUS_SET:
            return f"任务已结束（{record['status']}），无需取消。"
        changed = self._store.update_status_if_active(task_id, "cancelled", error="cancelled")
        running = self._running.get(task_id)
        if running is not None and not running.done():
            running.cancel()
        self._store.add_event(task_id, "cancelled")
        changes = self._store.list_changes(task_id=task_id)
        note = ""
        if changes:
            note = (
                f"\n⚠️ 该任务已记录 {len(changes)} 个文件变更，取消不会回滚已落盘的修改；"
                "可用 /task result 查看变更清单与备份位置。"
            )
        return (
            f"🛑 已取消任务 {task_id}。" if changed else f"任务状态已变化（{record['status']}）。"
        ) + note

    async def cancel_all_for(self, *, group_id: int, user_id: int) -> int:
        """撤销授权时取消该用户在本群的所有未完成任务（实时撤权生效）。"""
        cancelled = 0
        for record in self._store.list_recent(group_id=group_id, user_id=user_id, limit=50):
            if record["status"] in TERMINAL_STATUS_SET:
                continue
            if self._store.update_status_if_active(
                record["task_id"], "cancelled", error="access_revoked"
            ):
                self._store.add_event(record["task_id"], "cancelled", "access_revoked")
                cancelled += 1
                running = self._running.get(record["task_id"])
                if running is not None and not running.done():
                    running.cancel()
        if cancelled:
            logger.info("撤权取消任务数=%d group=%d user=%d", cancelled, group_id, user_id)
        return cancelled

    # ---------- 归档 ----------

    def _archive(
        self, *, request: TaskRequest, spec: ExecutionSpec, result: ExecutionResult, task_id: str
    ) -> None:
        directory = (self._settings.thinking_archive_dir or "").strip()
        if not directory:
            return
        if not result.thinking and not result.tool_log and not result.content:
            return
        try:
            target_dir = Path(directory)
            target_dir.mkdir(parents=True, exist_ok=True)
            document = (
                f"# task {task_id}\n\n"
                f"- 入口：{request.entry}\n"
                f"- Agent：{request.agent} · 后端：{spec.backend_id}\n"
                f"- 模型：{spec.model} · 强度：{spec.effort or '模型默认'}\n"
                f"- 模式：{spec.mode} · 目录：{spec.workspace or '（无）'}\n"
                f"- 群：{request.group_id} · 用户：{request.user_id}\n"
                f"- 时间：{datetime.now(ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M:%S}\n"
                f"- 状态：{result.error or '成功'}\n\n"
                f"- 任务：{request.prompt}\n\n"
            )
            if result.thinking:
                document += f"## 推理文本\n\n{result.thinking}\n\n"
            if result.content:
                document += f"## 最终答复\n\n{result.content}\n\n"
            if result.changes:
                document += "## 文件变更\n\n" + "\n".join(
                    f"- {change.kind} {change.path} [{change.status}] "
                    f"{change.before_hash[:8]}→{change.after_hash[:8]}"
                    + (f"（备份 {Path(change.backup).name}）" if change.backup else "")
                    for change in result.changes
                ) + "\n"
            if result.tool_log:
                document += "## 工具调用\n\n" + "\n".join(f"- {line}" for line in result.tool_log) + "\n"
            import os
            target = target_dir / f"{task_id}.md"
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                           encoding="utf-8") as output:
                output.write(self._redactor.redact(document))
        except OSError:
            logger.exception("task=%s 归档失败", task_id)

    # ---------- 授权辅助 ----------

    def can_use_mode(self, *, user_id: int, group_id: int, mode: str, workspace: Path | None) -> str:
        """返回空串表示允许，否则返回拒绝原因（供 /team plan 展示）。"""
        if mode == MODE_TEXT:
            return ""
        if workspace is None:
            return "缺少目录"
        decision = self._acl.authorize_path(
            user_id=user_id, group_id=group_id, path=workspace, mode=mode
        )
        return "" if decision.allowed else decision.reason

    def mode_allows(self, granted: str, requested: str) -> bool:
        return mode_allows(granted, requested)


__all__ = ["NOT_FOUND_TEXT", "RATE_LIMITED_TEXT", "TaskOutcome", "TaskService"]
