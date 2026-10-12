"""协作任务服务：所有权、配额、能力与目录冻结、状态查询与生命周期语义。

- 所有命令按 `group_id + owner_user_id` 鉴权；管理员不自动获得他人任务权限，
  不可访问与不存在返回同一文案，避免侧信道；
- run 时冻结每步 backend/model/effort/mode 与**规范目录**（显式覆盖 > 保存选择 >
  Agent 默认）；之后 /agent set 或目录绑定变化都不影响本轮；
- 目录关键词（--find）必须在 run 之前由本人选定；未选定则计划保持 draft，
  不会部分启动；
- 生命周期幂等：重复 run 不新建轮次、重复 cancel 返回当前状态、retry 仅限
  failed/interrupted 且复用成功结果；已取消任务不能 retry；
- 不自动重试任何模型调用。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from app.security.confirmations import (
    ConfirmationKind,
    InMemoryConfirmationStore,
    PendingAction,
)
from app.security.paths import PathBoundaryError, canonical
from app.workflows.models import (
    FrozenChoice,
    FrozenStep,
    PlanSnapshot,
    StepStatus,
    TeamStatus,
    find_undefined_dependencies,
    topological_layers,
)
from app.workflows.parser import TeamCommand
from app.workflows.runner import WorkflowRunner, error_text
from app.workflows.store import WorkflowStore, snapshot_from_json

logger = logging.getLogger("qqbot.workflows.service")

NOT_FOUND_TEXT = "⛔ 任务不存在或不属于你。"
RATE_LIMITED_TEXT = "⏳ 请求过于频繁，请稍后再试。"

_STATUS_LABEL = {
    TeamStatus.DRAFT: "草稿",
    TeamStatus.QUEUED: "排队中",
    TeamStatus.RUNNING: "执行中",
    TeamStatus.SUCCEEDED: "已成功",
    TeamStatus.FAILED: "已失败",
    TeamStatus.CANCELLED: "已取消",
    TeamStatus.INTERRUPTED: "已中断（服务重启）",
}

_STEP_LABEL = {
    StepStatus.PENDING: "待运行",
    StepStatus.READY: "就绪",
    StepStatus.RUNNING: "运行中",
    StepStatus.SUCCEEDED: "成功",
    StepStatus.FAILED: "失败",
    StepStatus.CANCELLED: "已取消",
    StepStatus.INTERRUPTED: "已中断",
    StepStatus.BLOCKED: "受阻",
}

_MODE_LABEL = {
    MODE_TEXT: "文本（无工具）",
    MODE_READ: "只读",
    MODE_EDIT: "读写",
    MODE_EXEC: "读写+受控命令",
}


def status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status)


def step_label(status: str) -> str:
    return _STEP_LABEL.get(status, status)


def mode_label(mode: str) -> str:
    return _MODE_LABEL.get(mode, mode)


class WorkflowService:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        runner: WorkflowRunner,
        registry,
        settings,
        authz,
        rate_limiter,
        tasks=None,
        workspaces=None,
        confirmations: InMemoryConfirmationStore | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._registry = registry
        self._settings = settings
        self._authz = authz
        self._rate_limiter = rate_limiter
        self._tasks = tasks
        self._workspaces = workspaces
        self._confirmations = confirmations

    # ---------- 入口 ----------

    async def execute(self, command: TeamCommand, message) -> str:
        if not self._settings.team_enabled:
            return "多 Agent 协作未启用。"
        handlers = {
            "create": self._create,
            "add": self._add,
            "remove": self._remove,
            "plan": self._plan,
            "run": self._run,
            "status": self._status,
            "result": self._result,
            "cancel": self._cancel,
            "retry": self._retry,
        }
        handler = handlers[command.action]
        return await handler(command, message)

    # ---------- 鉴权辅助 ----------

    def _owned(self, team_id: str, message):
        workflow = self._store.get_workflow(team_id)
        if workflow is None:
            return None
        if workflow.group_id != message.group_id or workflow.owner_user_id != message.user_id:
            return None
        return workflow

    async def revoke_access(self, group_id: int, user_id: int) -> int:
        cancelled = 0
        for workflow in self._store.list_active_teams(
            group_id=group_id, owner_user_id=user_id
        ):
            if await self._runner.cancel_team(workflow.team_id):
                cancelled += 1
                logger.info(
                    "授权撤销：已取消 team=%s group=%d user=%d",
                    workflow.team_id, group_id, user_id,
                )
        return cancelled

    # ---------- create / add / remove ----------

    async def _create(self, command: TeamCommand, message) -> str:
        record = self._store.create_draft(
            group_id=message.group_id, owner_user_id=message.user_id, goal=command.goal
        )
        agents = ", ".join(
            agent.name for agent in self._registry.configured_agents()
        ) or "（当前没有已配置的 Agent）"
        return (
            f"✅ 已创建团队任务 {record.team_id}\n"
            f"目标：{record.goal}\n"
            f"已配置 Agent：{agents}\n"
            f"下一步：/team add {record.team_id} <步骤ID> <agent> [--after a,b] "
            f"[--mode text|read|edit|exec] [--dir \"路径\"|--find \"关键词\"] -- <本步任务>\n"
            f"（不写 mode/dir 的步骤是纯文本；给出目录选择器时默认 read）"
        )

    async def _add(self, command: TeamCommand, message) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        if workflow.status != TeamStatus.DRAFT:
            return f"任务已启动，不能再修改步骤（当前状态：{status_label(workflow.status)}）。"
        agent = self._registry.get(command.agent)
        if agent is None:
            return (
                f"未知 Agent：{command.agent}；可选："
                + ", ".join(self._registry.all_agent_names())
            )
        if not agent.enabled:
            return f"Agent 已禁用：{command.agent}"
        if not self._store.has_step(command.team_id, command.step_id):
            if self._store.count_steps(command.team_id) >= self._settings.team_max_steps:
                return f"步骤数已达上限 {self._settings.team_max_steps}。"
        mode = command.mode or (
            MODE_READ if (command.directory or command.find) else MODE_TEXT
        )
        if mode == MODE_TEXT and (command.directory or command.find):
            return "--mode text 不接受 --dir/--find。"
        # 冻结选择校验（能力/模型/强度）
        choice, error = self._tasks.resolve_selection(
            agent_name=command.agent, group_id=message.group_id, user_id=message.user_id,
            model=command.model, effort=command.effort, effort_set=command.effort_set,
            backend_id=command.backend,
        )
        if choice is None:
            return f"步骤 {command.step_id} 的模型/后端不可用：{error}"
        backend_id, model, effort = choice
        workspace: str | None = None
        find_keyword: str | None = None
        notes: list[str] = []
        if command.directory is not None:
            resolved, message_text = self._resolve_directory(
                command.directory, mode=mode, message=message
            )
            if resolved is None:
                return f"步骤 {command.step_id} 的目录不可用：{message_text}"
            workspace = str(resolved)
        elif command.find is not None:
            find_keyword = command.find
        # 先写草稿（会推进 draft_version），再基于**最新版本**提出目录候选确认，
        # 否则新确认会被自己的"草稿变化使旧确认失效"逻辑误杀。
        created = self._store.replace_step(
            team_id=command.team_id, step_id=command.step_id, agent=command.agent,
            instruction=command.instruction, depends_on=command.after,
            model=command.model, effort=command.effort, effort_set=command.effort_set,
            mode=mode, backend=backend_id, workspace=workspace, find_keyword=find_keyword,
        )
        draft_version = self._store.draft_version(command.team_id)
        if find_keyword is not None:
            _keyword, note = self._propose_find(
                keyword=find_keyword, mode=mode, team_id=command.team_id,
                step_id=command.step_id, message=message,
            )
            if not note or "找到" not in note:
                return f"步骤 {command.step_id} 无法查找目录：{note}"
            notes.append(note)
        # 旧草稿版本的确认失效（新提出的确认带当前版本，不受影响）
        if self._workspaces is not None:
            self._workspaces.invalidate_for_draft(command.team_id, draft_version)
        definition = self._describe_definition(
            command, step_id=command.step_id, mode=mode, backend=backend_id,
            workspace=workspace, model=model, effort=effort,
        )
        undefined = [
            dep for dep in command.after if not self._store.has_step(command.team_id, dep)
        ]
        if undefined:
            notes.append(f"⚠️ 依赖 {', '.join(undefined)} 尚未定义；run 前必须补齐。")
        backend_runtime = self._tasks.registry.get(backend_id)
        if backend_runtime is not None and not backend_runtime.descriptor.supports(mode):
            notes.append(
                "⚠️ " + backend_runtime.descriptor.capability_error(mode)
            )
        if not self._registry.get(command.agent).configured:
            notes.append(
                f"⚠️ {self._registry.get(command.agent).display_name} 当前未配置："
                f"{self._registry.get(command.agent).status_note or '缺少配置'}。"
            )
        suffix = ("\n" + "\n".join(notes)) if notes else ""
        verb = "已添加" if created else "已完整替换"
        return f"✅ {verb}步骤 {command.step_id}：{definition}{suffix}"

    def _resolve_directory(self, raw: str, *, mode: str, message):
        if raw.strip() == ".":
            bound = self._workspaces.binding(
                group_id=message.group_id, user_id=message.user_id
            )
            if bound is None:
                return None, "--dir . 需要先绑定目录（/workspace find 或 /workspace use）"
            return bound, ""
        try:
            path = canonical(raw)
        except PathBoundaryError as exc:
            return None, str(exc)
        if not path.is_dir():
            return None, f"目录不存在：{path}"
        decision = self._tasks._acl.authorize_path(  # noqa: SLF001
            user_id=message.user_id, group_id=message.group_id, path=path, mode=mode
        )
        if not decision.allowed:
            return None, decision.reason
        return path, ""

    def _propose_find(self, *, keyword: str, mode: str, team_id: str, step_id: str, message):
        resolution = self._workspaces_resolve(
            keyword=keyword, mode=mode, team_id=team_id, step_id=step_id, message=message
        )
        if resolution is None:
            return None, "目录服务不可用"
        if resolution.awaiting_selection:
            return keyword, resolution.message
        return None, resolution.message or "未找到候选目录"

    def _workspaces_resolve(self, *, keyword: str, mode: str, team_id: str, step_id: str, message):
        """用统一的 WorkspaceService 提出候选（绑定 team/step/草稿版本）。"""
        from app.commands.options import PublicOptions

        options = PublicOptions(find=keyword, mode=mode)
        return self._workspaces.resolve_selector(
            group_id=message.group_id, user_id=message.user_id, options=options, mode=mode,
            draft_version=self._store.draft_version(team_id),
            resume_payload={"team_id": team_id, "step_id": step_id, "purpose": "team"},
            purpose="team",
        )

    def _describe_definition(
        self, item, *, step_id: str, mode: str | None = None, backend: str | None = None,
        workspace: str | None = None, model: str | None = None, effort: str | None = None,
    ) -> str:
        dependencies = getattr(item, "depends_on", None)
        if dependencies is None:
            dependencies = getattr(item, "after", ())
        parts = [f"{step_id} → {item.agent}"]
        effective_mode = mode if mode is not None else getattr(item, "mode", MODE_TEXT)
        parts.append(f"权限 {mode_label(effective_mode)}")
        chosen_backend = backend if backend is not None else getattr(item, "backend", None)
        if chosen_backend:
            parts.append(f"后端 {chosen_backend}")
        chosen_model = model if model is not None else getattr(item, "model", None)
        if chosen_model:
            parts.append(f"模型 {chosen_model}")
        chosen_effort = effort if effort is not None else getattr(item, "effort", None)
        if getattr(item, "effort_set", False) or chosen_effort:
            parts.append(f"强度 {chosen_effort or '模型默认'}")
        target_dir = workspace if workspace is not None else getattr(item, "workspace", None)
        keyword = getattr(item, "find_keyword", None)
        if target_dir:
            parts.append(f"目录 {target_dir}")
        elif keyword:
            parts.append(f"目录待选择（关键词 {keyword}）")
        parts.append("依赖 " + (",".join(dependencies) if dependencies else "-"))
        return " · ".join(parts)

    async def _remove(self, command: TeamCommand, message) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        try:
            self._store.remove_step(command.team_id, command.step_id)
        except LookupError as exc:
            if str(exc) == "step_not_found":
                return f"步骤不存在：{command.step_id}"
            return NOT_FOUND_TEXT
        except RuntimeError:
            return f"任务已启动，不能再删除步骤（当前状态：{status_label(workflow.status)}）。"
        except ValueError as exc:
            dependents = str(exc).removeprefix("dependents:")
            return f"仍有步骤依赖 {command.step_id}：{dependents}；请先调整这些步骤的 --after。"
        if self._workspaces is not None:
            self._workspaces.invalidate_for_draft(
                command.team_id, self._store.draft_version(command.team_id)
            )
        return f"🗑 已删除步骤 {command.step_id}。"

    # ---------- plan / run ----------

    async def _plan(self, command: TeamCommand, message) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        steps = self._store.list_steps(command.team_id)
        if not steps:
            return f"团队任务 {command.team_id} 还没有步骤。\n" + self._add_usage(command.team_id)
        dependencies = self._store.dependencies(command.team_id)
        step_ids = [step.step_id for step in steps]
        lines = [
            f"📋 团队任务 {command.team_id}（{status_label(workflow.status)}）",
            f"目标：{workflow.goal}",
            f"步骤（共 {len(steps)} 步）：",
        ]
        blocks: list[str] = []
        for index, step in enumerate(steps, start=1):
            lines.append(f"{index}. {self._describe_definition(step, step_id=step.step_id)}")
            blocked = self._precheck_step(step, message)
            if blocked:
                lines.append(f"    ⛔ {blocked}")
            elif step.workspace:
                blocks.append(step.workspace)
        missing = find_undefined_dependencies(step_ids, dependencies)
        if missing:
            lines.append("⚠️ 未定义依赖：" + "；".join(f"{a} ← {b}" for a, b in missing))
        layers = topological_layers(step_ids, dependencies)
        if layers is None:
            lines.append("❌ 依赖存在环或自环，run 前必须修复。")
        else:
            lines.append(
                "可并行层次：" + " | ".join(
                    f"第{i}层 [{', '.join(layer)}]" for i, layer in enumerate(layers, start=1)
                )
            )
        shared = {path for path in blocks if blocks.count(path) > 1}
        if shared:
            lines.append(
                "⚠️ 同目录步骤互斥："
                + "、".join(sorted(Path(name).name for name in shared))
                + "；需要确定先后顺序请给后面的步骤加 --after（锁不代替业务依赖）。"
            )
        pending = [step.step_id for step in steps if step.find_keyword]
        if pending:
            lines.append(
                "⏳ 以下步骤的目录尚未选定，run 前必须先用 /workspace select 选择："
                + "、".join(pending)
            )
        lines.append(
            f"执行参数：全局步骤并发 {self._settings.team_max_parallel_steps} · "
            f"单步超时 {self._settings.team_step_timeout():.0f} 秒 · "
            f"总时限 {int(self._settings.team_workflow_timeout_seconds)} 秒"
        )
        lines.append(f"启动：/team run {command.team_id}")
        return "\n".join(lines)

    def _precheck_step(self, step, message) -> str:
        """plan 阶段的能力/权限预检：只展示，不授予任何权限。"""
        if step.find_keyword:
            return f"目录待选择（关键词 {step.find_keyword}）"
        if step.mode != MODE_TEXT and not step.workspace:
            return f"{mode_label(step.mode)} 模式缺少目录"
        if step.workspace:
            reason = self._tasks.can_use_mode(
                user_id=message.user_id, group_id=message.group_id, mode=step.mode,
                workspace=self._safe_path(step.workspace),
            )
            if reason:
                return reason
        backend, error = self._tasks.registry.resolve(step.agent, step.backend)
        if backend is None:
            return error
        if not backend.descriptor.supports(step.mode):
            return backend.descriptor.capability_error(step.mode)
        if not backend.descriptor.configured:
            return f"后端 {backend.id} 未配置：{backend.descriptor.unavailable_reason}"
        return ""

    @staticmethod
    def _safe_path(raw: str):
        return Path(raw)

    async def _run(self, command: TeamCommand, message) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        if workflow.status != TeamStatus.DRAFT:
            if workflow.status in {TeamStatus.FAILED, TeamStatus.INTERRUPTED}:
                return (
                    f"任务当前状态：{status_label(workflow.status)}；"
                    f"如需重跑请用 /team retry {command.team_id}。"
                )
            return f"任务当前状态：{status_label(workflow.status)}，无需重复启动。"
        steps = self._store.list_steps(command.team_id)
        if not steps:
            return f"任务还没有步骤。\n" + self._add_usage(command.team_id)
        if len(steps) > self._settings.team_max_steps:
            return f"步骤数超过上限 {self._settings.team_max_steps}。"
        step_ids = [step.step_id for step in steps]
        dependencies = self._store.dependencies(command.team_id)
        missing = find_undefined_dependencies(step_ids, dependencies)
        if missing:
            return "依赖未定义：" + "；".join(f"{a} ← {b}" for a, b in missing) + "。请先 add 这些步骤。"
        if topological_layers(step_ids, dependencies) is None:
            return "依赖存在环或自环，已拒绝启动；请用 /team plan 检查。"
        pending = [step.step_id for step in steps if step.find_keyword]
        if pending:
            return (
                "以下步骤的目录尚未选择，未开始任何一步："
                + "、".join(pending)
                + "。请先按回执发送 /workspace select <确认码> <序号>，或用 /team add 重新指定 --dir。"
            )
        frozen, error = self._freeze(message, steps)
        if error is not None:
            return error
        if self._store.count_active_global() >= self._settings.team_max_active_global:
            return f"全局活动任务已达上限 {self._settings.team_max_active_global}，请稍后再试。"
        if self._store.count_active_user(message.user_id) >= self._settings.team_max_active_per_user:
            return (
                f"你已有 {self._settings.team_max_active_per_user} 个活动任务，"
                f"请等待完成或 /team cancel 后再启动。"
            )
        if not self._rate_limiter.allow(message.user_id):
            return RATE_LIMITED_TEXT
        snapshot = PlanSnapshot(
            team_id=command.team_id, goal=workflow.goal, steps=tuple(frozen)
        )
        round_no = self._store.freeze_run(command.team_id, snapshot)
        if round_no is None:
            current = self._store.get_workflow(command.team_id)
            status = status_label(current.status) if current else "未知"
            return f"任务状态已变化（当前：{status}），未重复启动。"
        if not await self._runner.start_team(command.team_id, round_no):
            self._store.interrupt_round(command.team_id, round_no, reason="shutdown")
            return "服务正在关闭，任务未启动；重启后可用 /team retry 恢复。"
        return self._run_receipt(command.team_id, round_no, snapshot)

    def _freeze(self, message, steps) -> tuple[list[FrozenStep], str | None]:
        frozen: list[FrozenStep] = []
        for step in steps:
            agent = self._registry.get(step.agent)
            if agent is None:
                return [], f"步骤 {step.step_id} 的 Agent 不存在：{step.agent}"
            if not agent.enabled:
                return [], f"步骤 {step.step_id} 的 Agent 已禁用：{step.agent}"
            choice, error = self._tasks.resolve_selection(
                agent_name=step.agent, group_id=message.group_id, user_id=message.user_id,
                model=step.model, effort=step.effort, effort_set=step.effort_set,
                backend_id=step.backend,
            )
            if choice is None:
                return [], f"步骤 {step.step_id} 模型/后端不可用：{error}"
            backend_id, model, effort = choice
            backend = self._tasks.registry.get(backend_id)
            if not backend.descriptor.configured:
                return [], (
                    f"步骤 {step.step_id} 的后端 {backend_id} 尚未配置："
                    f"{backend.descriptor.unavailable_reason}"
                )
            if not backend.descriptor.supports(step.mode):
                return [], f"步骤 {step.step_id}：" + backend.descriptor.capability_error(step.mode)
            grant_label = ""
            workspace = step.workspace
            if step.mode != MODE_TEXT:
                if not workspace:
                    return [], f"步骤 {step.step_id} 的 {mode_label(step.mode)} 缺少目录"
                try:
                    # 先规范路径，再判定与冻结（不随绑定或符号链接变化）
                    workspace = str(canonical(workspace))
                except PathBoundaryError as exc:
                    return [], f"步骤 {step.step_id}：{exc}"
                decision = self._tasks._acl.authorize_path(  # noqa: SLF001
                    user_id=message.user_id, group_id=message.group_id,
                    path=self._safe_path(workspace), mode=step.mode,
                    backend_modes=backend.descriptor.effective_modes(),
                )
                if not decision.allowed:
                    return [], f"步骤 {step.step_id}：{decision.reason}"
                grant_label = decision.grant.label if decision.grant else ""
            frozen.append(FrozenStep(
                step_id=step.step_id, seq=step.seq, agent=step.agent,
                instruction=step.instruction, depends_on=step.depends_on,
                choice=FrozenChoice(model=model, effort=effort, effort_set=step.effort_set),
                mode=step.mode, backend=backend_id, workspace=workspace,
                grant_label=grant_label,
            ))
        return frozen, None

    def _run_receipt(self, team_id: str, round_no: int, snapshot: PlanSnapshot) -> str:
        lines = [
            f"🚀 团队任务 {team_id} 已排入后台执行（第 {round_no} 轮）。",
            f"目标：{snapshot.goal}",
            f"步骤（{len(snapshot.steps)} 步，已冻结后端/模型/强度/权限/目录）：",
        ]
        for index, step in enumerate(snapshot.steps, start=1):
            lines.append(
                f"{index}. {step.step_id} → {step.agent} · 后端 {step.backend} · "
                f"模型 {step.choice.model} · 强度 {step.choice.effort or '模型默认'} · "
                f"权限 {mode_label(step.mode)}"
                + (f" · 目录 {step.workspace}" if step.workspace else "")
            )
        lines.append(
            f"单步超时 {self._settings.team_step_timeout():.0f} 秒 · "
            f"总时限 {int(self._settings.team_workflow_timeout_seconds)} 秒 · "
            f"步骤并发 {self._settings.team_max_parallel_steps}"
        )
        lines.append(f"查询：/team status {team_id}；取结果：/team result {team_id}")
        return "\n".join(lines)

    # ---------- 目录选定回调 ----------

    async def on_workspace_selected(self, outcome, message) -> str:
        """团队步骤的目录被选定：写入草稿并提示下一步。"""
        workflow = self._owned(outcome.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        if workflow.status != TeamStatus.DRAFT:
            return f"任务已启动，不能再修改步骤（当前状态：{status_label(workflow.status)}）。"
        current_version = self._store.draft_version(outcome.team_id)
        if outcome.draft_version and outcome.draft_version != current_version:
            return (
                "该确认码是旧草稿版本（草稿已被修改），已失效；"
                f"请重新 /team add 或用 /team plan 检查 {outcome.team_id}。"
            )
        if not self._store.has_step(outcome.team_id, outcome.step_id):
            return f"步骤不存在：{outcome.step_id}"
        steps = {step.step_id: step for step in self._store.list_steps(outcome.team_id)}
        step = steps.get(outcome.step_id)
        updated = self._store.set_step_workspace(
            team_id=outcome.team_id, step_id=outcome.step_id,
            workspace=str(outcome.workspace), mode=step.mode if step else None,
        )
        if not updated:
            return f"步骤 {outcome.step_id} 写入目录失败。"
        return (
            f"✅ 步骤 {outcome.step_id} 已冻结目录：{outcome.workspace}\n"
            f"其余步骤的目录仍需分别选择；全部选定后 /team run {outcome.team_id}"
        )

    # ---------- status / result ----------

    async def _status(self, command: TeamCommand, message) -> str:
        if not command.team_id:
            recent = self._store.list_recent_teams(
                group_id=message.group_id, owner_user_id=message.user_id, limit=10
            )
            if not recent:
                return "你在这个群还没有团队任务。用 /team create <目标> 创建。"
            lines = ["🗂 你最近在本群的团队任务（最多 10 个）："]
            for record in recent:
                lines.append(
                    f"· {record.team_id} · {status_label(record.status)} · {record.goal[:30]}"
                )
            return "\n".join(lines)
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        lines = [
            f"📊 团队任务 {command.team_id} · {status_label(workflow.status)}"
            + (f"（第 {workflow.round} 轮）" if workflow.round else ""),
            f"目标：{workflow.goal}",
        ]
        if workflow.round == 0:
            lines.append("尚未启动；可用 /team plan 查看计划。")
            return "\n".join(lines)
        runs = self._store.list_step_runs(command.team_id, workflow.round)
        steps = self._store.list_steps(command.team_id)
        for index, step in enumerate(steps, start=1):
            record = runs.get(step.step_id)
            if record is None:
                lines.append(f"{index}. {step.step_id} → {step.agent} · 无记录")
                continue
            detail = (
                f"{index}. {step.step_id} → {step.agent} · {step_label(record.status)}"
                f" · {mode_label(step.mode)}"
            )
            if record.model:
                detail += f" · {record.model}/{record.effort or '默认'}"
            if record.changed_files:
                detail += f" · 变更 {record.changed_files} 个文件"
            if record.status == StepStatus.SUCCEEDED:
                if record.source_round is not None:
                    detail += f"（复用第 {record.source_round} 轮结果）"
                else:
                    detail += f"（{record.duration_ms / 1000:.1f} 秒）"
            elif record.error:
                detail += f" · {error_text(record.error)}"
                if record.detail:
                    detail += f"（{record.detail}）"
            lines.append(detail)
        lines.append(f"结果：/team result {command.team_id}")
        return "\n".join(lines)

    async def _result(self, command: TeamCommand, message) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        if workflow.round == 0:
            return "任务尚未执行，暂无结果。"
        steps = self._store.list_steps(command.team_id)
        runs = self._store.list_step_runs(command.team_id, workflow.round)
        if command.result_step:
            if not any(step.step_id == command.result_step for step in steps):
                return f"步骤不存在：{command.result_step}"
            record = runs.get(command.result_step)
            if record is None:
                return f"步骤 {command.result_step} 本轮无记录。"
            return self._render_step_result(command.team_id, command.result_step, record)
        blocks: list[str] = [
            f"📄 团队任务 {command.team_id}（第 {workflow.round} 轮 · "
            f"{status_label(workflow.status)}）结果，按计划顺序："
        ]
        for step in steps:
            record = runs.get(step.step_id)
            if record is None:
                continue
            blocks.append("—— " + self._render_step_result(command.team_id, step.step_id, record))
        if len(blocks) == 1:
            return f"团队任务 {command.team_id} 本轮暂无结果。"
        return "\n\n".join(blocks)

    def _render_step_result(self, team_id: str, step_id: str, record) -> str:
        head = f"[{team_id}/{step_id}] {step_label(record.status)}"
        if record.source_round is not None:
            head += f"（复用第 {record.source_round} 轮）"
        if record.changed_files:
            head += f" · 已变更 {record.changed_files} 个文件"
        if record.status == StepStatus.SUCCEEDED and record.answer:
            return f"{head}\n{record.answer}"
        if record.error:
            suffix = f"（{record.detail}）" if record.detail else ""
            return f"{head} · {error_text(record.error)}{suffix}"
        return head

    # ---------- cancel / retry ----------

    async def _cancel(self, command: TeamCommand, message) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        if workflow.status == TeamStatus.DRAFT:
            self._store.cancel_draft(command.team_id)
            return f"🛑 已取消草稿 {command.team_id}。"
        if workflow.status in TeamStatus.ACTIVE:
            await self._runner.cancel_team(command.team_id)
            current = self._store.get_workflow(command.team_id)
            state = status_label(current.status) if current else "已取消"
            return (
                f"🛑 已取消团队任务 {command.team_id}（当前状态：{state}）。"
                f"已完成步骤的结果保留，可用 /team result 查询；"
                f"已产生的文件变更不会自动回滚。"
            )
        if workflow.status == TeamStatus.CANCELLED:
            return f"任务已处于取消状态（{command.team_id}），无需重复取消。"
        return f"任务已结束（{status_label(workflow.status)}），无需取消。"

    async def _retry(self, command: TeamCommand, message, *, force: bool = False) -> str:
        workflow = self._owned(command.team_id, message)
        if workflow is None:
            return NOT_FOUND_TEXT
        if workflow.status == TeamStatus.CANCELLED:
            return "已取消的任务不支持 retry；请用 /team create 新建计划。"
        if workflow.status not in {TeamStatus.FAILED, TeamStatus.INTERRUPTED}:
            return f"仅失败或中断的任务可 retry（当前状态：{status_label(workflow.status)}）。"
        run = self._store.get_run(command.team_id, workflow.round)
        if run is None:
            return "找不到上一轮计划快照，无法复用；请新建计划。"
        snapshot = snapshot_from_json(run.plan_json)
        # 1) 冻结选择仍然可用？
        for step in snapshot.steps:
            backend, error = self._tasks.registry.resolve(step.agent, step.backend)
            if backend is None or not backend.descriptor.enabled:
                return f"步骤 {step.step_id} 的后端已不可用（{error or step.agent}）；请新建计划。"
            if not backend.descriptor.configured:
                return (
                    f"步骤 {step.step_id} 的后端 {backend.id} 已不可用："
                    f"{backend.descriptor.unavailable_reason}；请新建计划。"
                )
            if not backend.descriptor.supports(step.mode):
                return f"步骤 {step.step_id}：" + backend.descriptor.capability_error(step.mode)
            if step.mode != MODE_TEXT and step.workspace:
                decision = self._tasks._acl.authorize_path(  # noqa: SLF001
                    user_id=message.user_id, group_id=message.group_id,
                    path=self._safe_path(step.workspace), mode=step.mode,
                    backend_modes=backend.descriptor.effective_modes(),
                )
                if not decision.allowed:
                    return f"步骤 {step.step_id}：{decision.reason}"
        # 2) 已成功的文件/执行步骤：检查是否可安全复用
        drift, side_effects = self._inspect_retry_risks(command.team_id, workflow.round, snapshot)
        if drift:
            return (
                "⛔ 检测到工作区漂移，拒绝复用成功步骤：\n"
                + "\n".join(f"· {line}" for line in drift)
                + "\n请新建计划（/team create）或人工恢复文件后再试；不会偷偷覆盖或重跑成功步骤。"
            )
        if side_effects and not force:
            summary = "\n".join(f"· {line}" for line in side_effects)
            pending = self._confirmations.create(
                kind=ConfirmationKind.ACTION,
                user_id=message.user_id,
                group_id=message.group_id,
                task_id=command.team_id,
                summary=f"确认重跑可能产生副作用的步骤（{command.team_id}）",
                payload={"team_id": command.team_id, "round": workflow.round, "retry": True},
                action=PendingAction(
                    kind="team.retry",
                    payload={"team_id": command.team_id, "round": workflow.round},
                    summary=f"重跑 {command.team_id} 的失败步骤",
                ),
            )
            return (
                "⚠️ 以下失败/中断步骤可能已经产生副作用，无法证明可以安全重复：\n"
                f"{summary}\n"
                "确认具体重跑风险后，发送 /confirm "
                f"{pending.code} 继续（{int(pending.ttl_remaining())} 秒内有效）。"
            )
        return await self._start_retry(command.team_id, workflow.round, snapshot, message)

    def _inspect_retry_risks(self, team_id: str, prev_round: int, snapshot: PlanSnapshot):
        """返回（漂移说明, 副作用说明）。

        F10：既检查成功步骤的**输出**（写变更）是否漂移，也检查其**输入**（读取过的文件）
        是否在团队之外被改动；同团队其他步骤的合法写入不算漂移。
        """
        from app.workspaces.changes import file_hash

        drift: list[str] = []
        side_effects: list[str] = []
        # 本轮团队自身写出的版本：path -> {after_hash}
        internal_writes: dict[str, set[str]] = {}
        for other in snapshot.steps:
            other_record = self._store.get_step_run(team_id, prev_round, other.step_id)
            if other_record is None or not other_record.task_id:
                continue
            for change in self._tasks._store.list_changes(task_id=other_record.task_id):  # noqa: SLF001
                if change["status"] == "applied" and change["after_hash"]:
                    internal_writes.setdefault(str(change["path"]), set()).add(change["after_hash"])
        for step in snapshot.steps:
            record = self._store.get_step_run(team_id, prev_round, step.step_id)
            if record is None:
                continue
            if record.task_id:
                changes = self._tasks._store.list_changes(task_id=record.task_id)  # noqa: SLF001
            else:
                changes = []
            if record.status == StepStatus.SUCCEEDED:
                # 1) 输入漂移：本步读过的文件必须还是当时的内容
                fingerprints = dict(getattr(record, "input_fingerprint", {}) or {})
                if step.mode != MODE_TEXT:
                    if step.mode == MODE_READ and not fingerprints:
                        drift.append(
                            f"{step.step_id}：只读步骤没有输入指纹记录，无法证明未漂移"
                        )
                    for path_text, recorded in fingerprints.items():
                        path = self._safe_path(path_text)
                        if not path.is_file():
                            if recorded:
                                drift.append(f"{step.step_id}：输入 {path.name} 已被删除")
                            continue
                        current = file_hash(path)
                        if current == recorded:
                            continue
                        if current in internal_writes.get(str(path), set()):
                            continue  # 同团队下游的合法修改
                        drift.append(
                            f"{step.step_id}：输入 {path.name} 在团队之外被修改（读取结论已过期）"
                        )
                # 2) 输出漂移：本步写出的文件必须仍是它写下的版本
                if step.mode != MODE_TEXT:
                    for change in changes:
                        if change["status"] != "applied":
                            continue
                        path = self._safe_path(change["path"])
                        if not path.is_file():
                            drift.append(f"{step.step_id}：{path.name} 已被删除或被外部替换")
                            continue
                        if change["after_hash"] and file_hash(path) != change["after_hash"]:
                            # 同团队后续步骤的合法写入不计为漂移
                            if file_hash(path) in internal_writes.get(str(path), set()):
                                continue
                            drift.append(f"{step.step_id}：{path.name} 在上游步骤之后又被修改")
                continue
            # 失败/中断步骤：判断重跑风险
            if step.mode == MODE_EXEC and (record.commands or step.needs_workspace):
                listed = "、".join(record.commands) if record.commands else "（命令历史未记录）"
                side_effects.append(
                    f"{step.step_id}（exec）：上次运行过 {listed}，无法证明命令可安全重复"
                )
            partial = [change["path"] for change in changes if change["status"] == "applied"]
            if partial:
                side_effects.append(
                    f"{step.step_id}（{mode_label(step.mode)}）：已部分写入 "
                    + "、".join(self._safe_path(item).name for item in partial)
                )
        return drift, side_effects

    async def _start_retry(self, team_id: str, prev_round: int, snapshot, message) -> str:
        if self._store.count_active_global() >= self._settings.team_max_active_global:
            return f"全局活动任务已达上限 {self._settings.team_max_active_global}，请稍后再试。"
        if self._store.count_active_user(message.user_id) >= self._settings.team_max_active_per_user:
            return f"你已有 {self._settings.team_max_active_per_user} 个活动任务，请稍后再试。"
        if not self._rate_limiter.allow(message.user_id):
            return RATE_LIMITED_TEXT
        round_no = self._store.start_retry(team_id, snapshot)
        if round_no is None:
            current = self._store.get_workflow(team_id)
            status = status_label(current.status) if current else "未知"
            return f"任务状态已变化（当前：{status}），未重复启动。"
        if not await self._runner.start_team(team_id, round_no):
            self._store.interrupt_round(team_id, round_no, reason="shutdown")
            return "服务正在关闭，重试未启动；重启后可再次 /team retry。"
        reused = [
            step.step_id for step in snapshot.steps
            if (record := self._store.get_step_run(team_id, round_no, step.step_id))
            is not None and record.source_round is not None
        ]
        pending = [step.step_id for step in snapshot.steps if step.step_id not in reused]
        lines = [
            f"🔁 团队任务 {team_id} 已开启第 {round_no} 轮。",
            f"复用成功步骤：{', '.join(reused) if reused else '（无）'}",
            f"将重跑：{', '.join(pending) if pending else '（无）'}",
            f"查询：/team status {team_id}",
        ]
        return "\n".join(lines)

    async def handle_confirmation(self, record, message) -> str:
        """处理 team 相关的 /confirm（重跑已产生副作用的失败步骤）。"""
        payload = record.payload or {}
        team_id = str(payload.get("team_id") or "")
        if not team_id:
            return ""
        command = TeamCommand(action="retry", team_id=team_id)
        return await self._retry(command, message, force=True)

    # ---------- 文案辅助 ----------

    @staticmethod
    def _add_usage(team_id: str) -> str:
        return (
            f"添加步骤：/team add {team_id} <步骤ID> <agent> [--after a,b] "
            f"[--mode text|read|edit|exec] [--dir \"路径\"|--find \"关键词\"] -- <本步任务>"
        )
