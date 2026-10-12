"""统一任务提交服务：QQ 与 HTTP 共用同一套准备/续跑逻辑（F11）。

之前 HTTP 端点自己拼了一套"找目录→选目录→建任务"，与 QQ 的 Dispatcher 不一致：
选择完成后 HTTP 不会续跑原任务，也没有保存完整的 backend/model/effort/effort_set。
本模块把这段流程收敛到一处，两个入口都调用它：

- `options_from_payload()`：把 HTTP 参数按 QQ 的解析器语义校验（未知/冲突选项
  与 QQ 完全一致地拒绝），避免出现"HTTP 能提交 QQ 拒绝的组合"；
- `prepare()`：解析目录选择器 → 需要选择时落库 awaiting_selection 并返回确认信息，
  否则冻结规格返回可执行的 PreparedTask；
- `resume()`：确认码被消费后按原请求续跑一次（含 effort_set 等显式选择）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.backends.protocol import TaskRequest
from app.commands.options import OptionError, PublicOptions, parse_public_options
from app.commands.registry import CommandRegistry
from app.tasks.service import TaskOutcome
from app.tasks.store import IdempotencyKeyConflict
from app.workspaces.models import WorkspaceError

_ALLOWED_PAYLOAD_KEYS = {
    "agent", "prompt", "group_id", "mode", "dir", "find", "backend", "model",
    "effort", "entry", "team_id", "step_id",
}
# 身份永远以 Token 映射为准；这些字段出现在正文里只被忽略（不报错、更不生效）
_IGNORED_PAYLOAD_KEYS = {
    "user_id", "admin", "is_admin", "self_id", "group", "role", "scopes", "token",
}


class SubmissionError(ValueError):
    """提交被拒绝；message 可直接作为 400/回执文案。"""


def _task_spec():
    spec = CommandRegistry().get("task")
    if spec is None:  # pragma: no cover - 注册表必然包含 task
        raise RuntimeError("task 命令未注册")
    return spec


def options_from_payload(payload: dict) -> PublicOptions:
    """把结构化请求转成公共选项，并沿用 QQ 的解析与冲突校验。"""
    unknown = set(payload) - _ALLOWED_PAYLOAD_KEYS - _IGNORED_PAYLOAD_KEYS
    if unknown:
        raise SubmissionError(f"未知字段：{', '.join(sorted(unknown))}")
    parts: list[str] = []
    for name in ("backend", "model", "effort", "mode", "dir", "find"):
        value = payload.get(name)
        if value in (None, ""):
            continue
        parts += [f"--{name}", '"' + str(value).replace('"', "") + '"']
    header = " ".join(parts)
    try:
        options = parse_public_options(header, spec=_task_spec())
    except OptionError as exc:
        raise SubmissionError(str(exc)) from None
    return options


@dataclass(frozen=True, slots=True)
class PreparedTask:
    request: TaskRequest
    spec: object


@dataclass(frozen=True, slots=True)
class DuplicateSubmission:
    """同一 QQ 消息的重复投递：指向既有任务，不产生新的执行/目录选择/确认回执。"""

    existing: dict

    @property
    def task_id(self) -> str:
        return str(self.existing.get("task_id") or "")

    @property
    def status(self) -> str:
        return str(self.existing.get("status") or "")


@dataclass(frozen=True, slots=True)
class PendingSelection:
    task_id: str
    code: str
    candidates: tuple[str, ...]
    message: str
    # 该选择是否已按幂等键原子登记（HTTP 带 Idempotency-Key 时为 True）
    reserved: bool = False
    idempotency_key: str = ""


class TaskSubmissionService:
    def __init__(self, *, tasks, workspaces) -> None:
        self._tasks = tasks
        self._workspaces = workspaces

    # ---------- 准备 ----------

    def prepare(
        self, *, entry: str, group_id: int, user_id: int, agent: str,
        options: PublicOptions, use_chat_context: bool = True,
        idempotency_key: str | None = None, key_scheme: str | None = None,
    ) -> PreparedTask | PendingSelection | DuplicateSubmission:
        mode = options.resolved_mode()
        pending_id = self._tasks._store.new_id()  # noqa: SLF001 - ID 由存储层生成
        deduplicating = bool(idempotency_key)
        if deduplicating:
            # 先**原子登记**，再做任何目录选择/确认副作用：命中重复就直接返回既有任务。
            try:
                existing, created = self._tasks._store.register_idempotent_task(  # noqa: SLF001
                    entry=entry, group_id=group_id, user_id=user_id, agent=agent,
                    prompt=options.body, mode=mode, backend=options.backend,
                    model=options.model, effort=options.effort, effort_set=options.effort_set,
                    status="queued", idempotency_key=idempotency_key, task_id=pending_id,
                    key_scheme=key_scheme,
                )
            except IdempotencyKeyConflict as exc:
                # 所有存储候选都被**其它归属**的记录占用：明确拒绝，绝不冒充别人的记录
                raise SubmissionError(str(exc)) from None
            if not created:
                if existing is not None:
                    return DuplicateSubmission(existing=existing)
                # 原子裁决判定"不是新建"却读不到既有行（并发写尚未可见的极端情形）：
                # 宁可少执行——拒绝本次，不留下半登记行，绝不重复调用模型。
                raise SubmissionError(
                    "该请求与一条正在登记中的相同请求冲突，本次未重复执行；请稍后重试。"
                )
        try:
            return self._prepare_claimed(
                entry=entry, group_id=group_id, user_id=user_id, agent=agent, options=options,
                mode=mode, pending_id=pending_id, use_chat_context=use_chat_context,
                idempotency_key=idempotency_key, deduplicating=deduplicating,
            )
        except SubmissionError:
            # 拒绝路径已在内部按具体原因收敛终态并保留幂等键：原样上抛，不重复改写
            raise
        except Exception as exc:
            # 预登记之后的**任何**阶段（目录解析 / 待选择状态转换 / 对象构造 / build_spec…）
            # 抛出非预期异常：收敛为 failed 终态——只改仍活动的行，保留幂等键与行本身，
            # 只记脱敏诊断（异常类型），异常继续上抛（不吞、不自动重试）；
            # 绝不留下永久 queued/awaiting_selection，也绝不覆盖 cancelled/interrupted/succeeded。
            self._fail_reservation(pending_id, "internal_error", type(exc).__name__)
            raise

    def _prepare_claimed(
        self, *, entry: str, group_id: int, user_id: int, agent: str, options: PublicOptions,
        mode: str, pending_id: str, use_chat_context: bool, idempotency_key: str | None,
        deduplicating: bool,
    ) -> PreparedTask | PendingSelection:
        """预登记之后的全部阶段：目录解析 → 等待选择 → 冻结规格。

        本方法只处理"已登记（或无需登记）"之后的流程；重复命中与登记冲突已在 `prepare()`
        处理。非预期异常一律由 `prepare()` 的外层护栏收敛为 failed 终态。
        """
        try:
            resolution = self._workspaces.resolve_selector(
                group_id=group_id, user_id=user_id, options=options, mode=mode,
                task_id=pending_id,
                resume_payload={"task_id": pending_id, "purpose": "task"},
            )
        except WorkspaceError as exc:
            # 预登记成功之后抛出的目录错误：必须收敛为**拒绝终态**并保留幂等键，
            # 不能留下永久 queued 的预登记行（同一消息重放也不会因此重跑）。
            message = getattr(exc, "message", "") or str(exc)
            if deduplicating:
                self._fail_reservation(pending_id, "workspace_error", message)
            raise SubmissionError(message) from None
        except Exception as exc:
            # 非预期异常：同样收敛终态（保留幂等键），异常继续向上抛出，不吞掉、不自动重试
            if deduplicating:
                self._fail_reservation(pending_id, "unexpected_error", type(exc).__name__)
            raise
        if resolution.awaiting_selection:
            if deduplicating:
                # 状态转换只在行仍活动时生效：若预登记行已被并发取消/中断，保留既有终态
                # （不把取消说成"等待选择"），但仍补齐冻结字段。
                self._tasks._store.update_status_if_active(  # noqa: SLF001
                    pending_id, "awaiting_selection"
                )
                self._tasks._store.update_task(  # noqa: SLF001
                    pending_id, backend=options.backend, model=options.model, effort=options.effort,
                )
            else:
                self._tasks._store.create_task(  # noqa: SLF001
                    entry=entry, group_id=group_id, user_id=user_id, agent=agent,
                    prompt=options.body, mode=mode, backend=options.backend,
                    model=options.model, effort=options.effort,
                    effort_set=options.effort_set, status="awaiting_selection",
                    idempotency_key=idempotency_key, task_id=pending_id,
                )
            return PendingSelection(
                task_id=pending_id,
                code=resolution.selection_code,
                candidates=tuple(str(item.path) for item in resolution.candidates),
                message=resolution.message,
                reserved=deduplicating,
                idempotency_key=idempotency_key or "",
            )
        if resolution.workspace is None and resolution.message:
            if deduplicating:
                self._fail_reservation(pending_id, "invalid_option", resolution.message)
            raise SubmissionError(resolution.message)
        request = TaskRequest(
            entry=entry, group_id=group_id, user_id=user_id, agent=agent,
            prompt=options.body, mode=mode, backend=options.backend,
            model=options.model, effort=options.effort, effort_set=options.effort_set,
            workspace=resolution.workspace, use_chat_context=use_chat_context,
            idempotency_key=idempotency_key,
            # 已登记时把 pending_id 贯穿到执行阶段（_ensure_record 会复用同一行，
            # 不会产生第二条记录）；同时把幂等键交给任务记录，供后续查询与刷新。
            task_id=pending_id if deduplicating else None,
        )
        spec, error = self._tasks.build_spec(
            request=request, workspace=resolution.workspace, mode=mode
        )
        if spec is None:
            if deduplicating:
                self._fail_reservation(pending_id, "invalid_option", str(error))
            raise SubmissionError(error)
        return PreparedTask(request=request, spec=spec)

    def _fail_reservation(self, task_id: str, error: str, detail: str = "") -> None:
        """把预登记行收敛为 ``failed`` 终态：只改仍活动的行，绝不覆盖取消/中断等终态。

        幂等键与行本身都保留，因此同一提交再次投递只会命中这条终态记录，不会自动重跑。
        """
        if self._tasks._store.update_status_if_active(task_id, "failed", error=error):  # noqa: SLF001
            self._tasks._store.add_event(  # noqa: SLF001
                task_id, "rejected", f"{error}:{detail}"[:200]
            )

    # ---------- 续跑 ----------

    async def resume(
        self, *, task_id: str, group_id: int, user_id: int, workspace: Path,
    ) -> TaskOutcome | str:
        """目录确认后按原请求续跑一次；失败返回可回执文案。"""
        record = self._tasks.own_task(task_id, group_id=group_id, user_id=user_id)
        if record is None or record["status"] != "awaiting_selection":
            return "原任务已失效或不归你所有，未自动执行；请重新发起 /task。"
        try:
            request = TaskRequest(
                entry=record["entry"], group_id=group_id, user_id=user_id,
                agent=record["agent"], prompt=record["prompt"], mode=record["mode"],
                backend=record["backend"], model=record["model"], effort=record["effort"],
                effort_set=bool(record.get("effort_set")),
                workspace=workspace, use_chat_context=True, task_id=record["task_id"],
            )
            spec, error = self._tasks.build_spec(
                request=request, workspace=workspace, mode=record["mode"]
            )
            if spec is None:
                self._fail_reservation(record["task_id"], "invalid_option", str(error))
                return error
            # 状态转回 queued 只在行仍活动时生效：期间被并发取消/中断（终态）时不得复活执行。
            if not self._tasks._store.update_status_if_active(record["task_id"], "queued"):  # noqa: SLF001
                return "原任务已失效或不归你所有，未自动执行；请重新发起 /task。"
            return await self._tasks.run(
                request, spec=spec, history=self._tasks.history_for(request)
            )
        except Exception as exc:
            # 目录确认后的规格构建/状态转换/执行护栏异常：收敛 failed 终态（只改活动行、
            # 保留幂等键与行本身），绝不留永久 awaiting_selection/queued；
            # 只记脱敏诊断（异常类型），异常继续上抛（CancelledError 不属于 Exception，不受影响）。
            self._fail_reservation(record["task_id"], "internal_error", type(exc).__name__)
            raise
