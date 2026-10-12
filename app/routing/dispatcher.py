"""核心调度器：把 QQ 消息路由到注册表声明的命令族。

职责（与具体传输协议、模型厂商解耦）：
- 命令路由（唯一来源是 CommandRegistry）与权限校验；
- 旧口令只返回迁移提示，绝不执行任何操作；
- 每用户限流、防 bot 自我/互相回复循环；
- 把任务交给 TaskService（单任务/广播/team 步骤唯一执行入口）；
- 回复分段、脱敏与错误文案（不泄露密钥/请求头/堆栈）。

本模块不再实现任何文件读写：目录与文件操作全部在 WorkspaceService / 公共 broker。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path

from app.backends.protocol import ENTRY_QQ, TaskRequest
from app.backends.registry import BackendRegistry
from app.commands.options import (
    MODE_TEXT,
    OptionError,
    parse_public_options,
    parse_task_arguments,
)
from app.commands.registry import ParsedCommand
from app.commands.presets import PresetError, PresetSpec, expand_preset
from app.commands.registry import CommandRegistry
from app.models import InboundMessage
from app.security.confirmations import (
    ConfirmationKind,
    ConfirmationOutcome,
    InMemoryConfirmationStore,
)
from app.security.text_safety import sanitize_inbound_text, sanitize_outbound_text, split_message
from app.tasks.service import NOT_FOUND_TEXT, TaskOutcome, TaskService
from app.tasks.store import IdempotencyKeyConflict
from app.routing.qq_dedup import build_key, duplicate_reply
from app.tasks.submission import (
    DuplicateSubmission,
    PendingSelection,
    PreparedTask,
    SubmissionError,
    TaskSubmissionService,
)
from app.transports.base import Transport, TransportError
from app.workspaces.models import WorkspaceError
from app.workspaces.service import WorkspaceService

logger = logging.getLogger("qqbot.dispatcher")

_UNAUTHORIZED_TEXT = "⛔ 未授权：你不在本机器人的允许名单中，仅可使用 /help。"
_ADMIN_ONLY_TEXT = "此命令仅允许机器人管理员使用。"
_NOT_IN_GROUP_TEXT = "该群未授权使用本机器人。"
_USAGE_FALLBACK = "用法有误；发送 /help 或用 /command show <命令> 查看参数。"


class Dispatcher:
    def __init__(
        self,
        *,
        settings,
        registry: BackendRegistry,
        store,
        authz,
        rate_limiter,
        confirmations: InMemoryConfirmationStore,
        transport: Transport,
        redactor,
        tasks: TaskService,
        workspaces: WorkspaceService,
        acl=None,
        commands: CommandRegistry | None = None,
        presets: dict[str, PresetSpec] | None = None,
        team=None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.store = store
        self.authz = authz
        self.authz.store = store
        self.rate_limiter = rate_limiter
        self.confirmations = confirmations
        self.transport = transport
        self.redactor = redactor
        self.tasks = tasks
        self.workspaces = workspaces
        self.submission = TaskSubmissionService(tasks=tasks, workspaces=workspaces)
        self.acl = acl
        self.commands = commands or CommandRegistry()
        self.presets = dict(presets or {})
        self.team = team
        self._ignore_users = frozenset(settings.qq_ignore_user_ids)
        self._logger = logging.getLogger("qqbot.dispatcher")
        self._handlers = {
            "task": self._handle_task,
            "workspace": self._handle_workspace,
            "agent": self._handle_agent,
            "team": self._handle_team,
            "confirm": self._handle_confirm,
            "access": self._handle_access,
            "command": self._handle_command,
            "help": self._handle_help,
        }
        self.commands.bind_handlers(self._handlers)

    # ---------- 入口 ----------

    async def handle_onebot_event(self, event: dict) -> None:
        from app.transports.onebot_common import parse_onebot_group_message

        message = parse_onebot_group_message(event)
        if message is None:
            return
        await self.handle_message(message)

    async def handle_message(self, message: InboundMessage) -> None:
        if message.user_id == message.self_id:
            return
        if message.user_id in self._ignore_users:
            return
        if not self.authz.group_allowed(message.group_id):
            return
        text = sanitize_inbound_text(message.text)
        parsed = self.commands.parse(text)
        if parsed is None and text.strip().startswith("/"):
            parsed = self._maybe_preset(text)
        if parsed is None:
            return
        if isinstance(parsed, str):
            # 旧口令：只给迁移提示，不执行任何操作
            self._logger.info(
                "legacy_command user=%d group=%d text=%r",
                message.user_id, message.group_id, text[:40],
            )
            await self._send(message.group_id, parsed)
            return
        self._logger.info(
            "cmd=%s sub=%s user=%d group=%d authorized=%s",
            parsed.name, parsed.subcommand, message.user_id, message.group_id,
            self.authz.user_allowed(message.user_id, message.group_id),
        )
        if not self.authz.user_allowed(message.user_id, message.group_id):
            if parsed.name == "help":
                await self._send(message.group_id, self.commands.help_text(parsed.rest))
            else:
                await self._send(message.group_id, _UNAUTHORIZED_TEXT)
            return
        spec = self.commands.get(parsed.name)
        if spec is not None and spec.authorization == "admin" and not self.authz.admin_allowed(message.user_id):
            await self._send(message.group_id, _ADMIN_ONLY_TEXT)
            return
        if spec is not None:
            handler_key = spec.handler_key
        elif parsed.name in self.presets:
            # preset 展开为规范 TaskRequest，复用 task 处理器
            handler_key = "task"
        else:
            handler_key = ""
        handler = self._handlers.get(handler_key)
        if handler is None:
            await self._send(message.group_id, _USAGE_FALLBACK)
            return
        try:
            await handler(parsed, message)
        except Exception:
            self._logger.exception(
                "命令处理异常 cmd=%s user=%d group=%d",
                parsed.name, message.user_id, message.group_id,
            )
            await self._send(message.group_id, "⛔ 命令处理失败（内部错误，详情见服务端日志）。")

    def _maybe_preset(self, text: str) -> ParsedCommand | str | None:
        body = text.strip()[1:]
        head, _, rest = body.partition(" ")
        name = head.strip().lower()
        if name not in self.presets:
            return None
        return ParsedCommand(name=name, args=rest.strip(), subcommand="", rest=rest.strip())

    # ---------- /task ----------

    async def _handle_task(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        if parsed.subcommand == "status":
            await self._task_status(parsed.rest, message)
            return
        if parsed.subcommand == "result":
            await self._task_result(parsed.rest, message)
            return
        if parsed.subcommand == "cancel":
            await self._task_cancel(parsed.rest, message)
            return
        if parsed.subcommand == "all":
            await self._task_all(parsed.rest, message)
            return
        if parsed.name in self.presets:
            await self._run_preset(parsed, message)
            return
        await self._task_run(parsed.rest, message)

    def _task_spec(self):
        spec = self.commands.get("task")
        if spec is None:  # pragma: no cover - 注册表必然包含 task
            raise RuntimeError("task 命令未注册")
        return spec

    async def _task_run(self, args: str, message: InboundMessage) -> None:
        head, _, remainder = args.partition(" ")
        agent = head.strip().lower()
        if not agent:
            await self._send(message.group_id, self.commands.help_text("task"))
            return
        if self.registry.agent(agent) is None:
            await self._send(
                message.group_id,
                f"未知 Agent：{agent}；可选：" + ", ".join(self.registry.all_agent_names()),
            )
            return
        try:
            options = parse_task_arguments(remainder.strip(), spec=self._task_spec())
        except OptionError as exc:
            await self._send(message.group_id, str(exc))
            return
        await self._submit_task(
            agent=agent, options=options, message=message, entry=ENTRY_QQ,
        )

    async def _run_preset(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        preset = self.presets[parsed.name]
        spec = self.commands.get("task")
        try:
            registered = self.commands.get(parsed.name) or _preset_spec(preset, spec)
            options = parse_task_arguments(
                parsed.args, spec=registered, allow_after=False
            )
            expanded = expand_preset(preset, options)
        except (OptionError, PresetError) as exc:
            await self._send(message.group_id, str(exc))
            return
        if self.registry.agent(expanded.agent) is None:
            await self._send(
                message.group_id,
                f"preset /{preset.name} 绑定的 Agent 不存在：{expanded.agent}",
            )
            return
        await self._submit_task(
            agent=expanded.agent,
            options=_options_from_preset(expanded),
            message=message,
            entry=ENTRY_QQ,
            preset=preset.name,
        )

    def _bot_account(self) -> int | None:
        """机器人自身 QQ 账号：来自传输层记录（未知时返回 None，键里用 `-` 占位）。

        同一会话内该值在**首次**入站事件后即固定；因此先收到的消息与之后的消息
        使用同一个命名空间，不会因为登录信息稍后到达而产生两个键。
        """
        getter = getattr(self.transport, "bot_account", None)
        if callable(getter):
            try:
                account = getter()
            except Exception:  # noqa: BLE001 - 传输层异常不得影响提交
                account = None
            if isinstance(account, int) and account > 0:
                return account
        return None

    async def _submit_task(
        self, *, agent: str, options, message: InboundMessage, entry: str,
        use_chat_context: bool = True, preset: str = "", target: str | None = None,
    ) -> None:
        """解析目录选择器 -> 冻结规格 -> 执行（或返回候选等待选择）。

        与 HTTP 共用 TaskSubmissionService，保证两个入口行为完全一致。
        QQ 入口额外带上**带命名空间的消息幂等键**：同一条消息（相同 message_id、
        相同机器人/群/用户/提交目标）重复或并发投递只执行一次。
        鉴权在调用本方法之前完成，因此去重不会给未授权身份泄漏任务结果。
        """
        dedup = build_key(
            message, target=target or (f"preset:{preset}" if preset else "task"), agent=agent,
            bot_account=self._bot_account(),
        )
        try:
            prepared = self.submission.prepare(
                entry=entry, group_id=message.group_id, user_id=message.user_id,
                agent=agent, options=options, use_chat_context=use_chat_context,
                idempotency_key=dedup.value or None,
            )
        except SubmissionError as exc:
            await self._send(message.group_id, sanitize_outbound_text(str(exc)))
            return
        if isinstance(prepared, DuplicateSubmission):
            await self._send(message.group_id, sanitize_outbound_text(
                duplicate_reply(prepared.existing)))
            return
        if isinstance(prepared, PendingSelection):
            await self._send(message.group_id, sanitize_outbound_text(prepared.message))
            return
        outcome = await self.tasks.run(
            prepared.request, spec=prepared.spec,
            history=self.tasks.history_for(prepared.request),
        )
        await self._reply_outcome(outcome, message.group_id)

    async def _task_all(self, args: str, message: InboundMessage) -> None:
        try:
            options = parse_task_arguments(args, spec=self._task_spec())
        except OptionError as exc:
            await self._send(message.group_id, str(exc))
            return
        if options.given:
            await self._send(
                message.group_id,
                "/task all 不接受 --dir/--find/--mode/--model/--effort/--backend；"
                "每个 Agent 使用自己的默认文本配置。",
            )
            return
        agents = [agent.name for agent in self.registry.configured_agents()]
        if not agents:
            await self._send(
                message.group_id,
                "当前没有已配置的 Agent：请检查后端密钥与本机安装（/agent list 查看状态）。",
            )
            return
        if self.settings.shared_agent_context:
            outcomes = []
            for agent in agents:
                outcomes.append(await self._run_broadcast_one(agent, options.body, message))
        else:
            outcomes = list(await asyncio.gather(*(
                self._run_broadcast_one(agent, options.body, message) for agent in agents
            )))
        for outcome in outcomes:
            await self._reply_outcome(outcome, message.group_id)

    async def _run_broadcast_one(
        self, agent: str, prompt: str, message: InboundMessage
    ) -> TaskOutcome:
        request = TaskRequest(
            entry=ENTRY_QQ, group_id=message.group_id, user_id=message.user_id,
            agent=agent, prompt=prompt, mode=MODE_TEXT, use_chat_context=True,
        )
        # /task all 的**每个子任务**独立去重：同一 message_id 只投递一次时，
        # 每个 Agent 各执行一次；重复投递则各复用既有子任务。
        dedup = build_key(message, target="all", agent=agent,
                          bot_account=self._bot_account())
        if dedup.value:
            try:
                existing, created = self.tasks._store.register_idempotent_task(  # noqa: SLF001
                    entry=ENTRY_QQ, group_id=message.group_id, user_id=message.user_id,
                    agent=agent, prompt=prompt, mode=MODE_TEXT,
                    status="queued", idempotency_key=dedup.value,
                )
            except IdempotencyKeyConflict as exc:
                # 所有存储候选都被其它归属的记录占用：明确拒绝本次子任务，
                # 绝不把别人的记录当"重复投递"返回，也不覆盖任何既有行。
                return TaskOutcome(
                    task_id="", ok=False, status="failed", agent=agent,
                    display=self.tasks._display_name(agent),  # noqa: SLF001
                    error="idempotency_conflict", detail=str(exc), mode=MODE_TEXT,
                )
            if not created:
                if existing is not None:
                    # 成功路径渲染 `content`（不是 `detail`）：把"未重复执行"正文放进 content，
                    # 并带上既有任务真实的 backend/model，避免空回复让人误以为失败而重发。
                    return TaskOutcome(
                        task_id=str(existing.get("task_id") or ""), ok=True,
                        status=str(existing.get("status") or "queued"),
                        agent=agent, display=self.tasks._display_name(agent),  # noqa: SLF001
                        backend=str(existing.get("backend") or ""),
                        model=str(existing.get("model") or ""),
                        effort=existing.get("effort"),
                        content=duplicate_reply(existing),
                        detail=duplicate_reply(existing), mode=MODE_TEXT,
                    )
                # 原子裁决判定"不是新建"却读不到既有行（并发写尚未可见）：宁可少执行。
                return TaskOutcome(
                    task_id="", ok=False, status="failed", agent=agent,
                    display=self.tasks._display_name(agent),  # noqa: SLF001
                    error="conflict",
                    detail="该消息正在被并发登记，本次未重复执行；请稍后重试。",
                    mode=MODE_TEXT,
                )
            claimed_id = str(existing["task_id"])
        else:
            claimed_id = ""
        try:
            if claimed_id:
                request = dataclasses.replace(request, task_id=claimed_id)
            spec, error = self.tasks.build_spec(request=request, workspace=None, mode=MODE_TEXT)
        except Exception as exc:
            # 登记成功之后的构造/规格异常：收敛 failed 终态（只改仍活动的行、保留幂等键与行
            # 本身），只记脱敏诊断（异常类型），异常继续上抛——绝不留永久 queued；
            # 绝不覆盖并发取消/中断等既有终态（update_status_if_active 只在活动行生效）。
            if claimed_id:
                if self.tasks._store.update_status_if_active(  # noqa: SLF001
                    claimed_id, "failed", error="internal_error"
                ):
                    self.tasks._store.add_event(  # noqa: SLF001
                        claimed_id, "error", f"unexpected:{type(exc).__name__}"[:200]
                    )
            raise
        if spec is None:
            task_id = claimed_id
            status = "failed"
            outcome_error = "invalid_option"
            if claimed_id:
                # 预登记成功之后才失败的出口：只在行仍活动时收敛为失败终态并记录原因，
                # **绝不覆盖**并发取消/中断等既有终态（F4）；幂等键与行本身保留。
                if self.tasks._store.update_status_if_active(  # noqa: SLF001
                    task_id, "failed", error="invalid_option"
                ):
                    self.tasks._store.add_event(  # noqa: SLF001
                        task_id, "rejected", f"invalid_option:{error}"[:200]
                    )
                else:
                    current = self.tasks._store.get_task(task_id)  # noqa: SLF001
                    if current is not None:
                        status = str(current["status"])
                        outcome_error = str(current.get("error") or outcome_error)
            return TaskOutcome(
                task_id=task_id, ok=False, status=status, agent=agent,
                display=self.tasks._display_name(agent),  # noqa: SLF001
                error=outcome_error, detail=error, mode=MODE_TEXT,
            )
        return await self.tasks.run(request, spec=spec, history=self.tasks.history_for(request))

    async def _task_status(self, args: str, message: InboundMessage) -> None:
        task_id = args.strip()
        if task_id:
            record = self.tasks.own_task(
                task_id, group_id=message.group_id, user_id=message.user_id
            )
            if record is None:
                await self._send(message.group_id, NOT_FOUND_TEXT)
                return
            await self._send(message.group_id, self._render_task_status(record))
            return
        recent = self.tasks._store.list_recent(  # noqa: SLF001
            group_id=message.group_id, user_id=message.user_id, limit=10
        )
        if not recent:
            await self._send(message.group_id, "你在这个群还没有任务。")
            return
        lines = ["🗂 你最近在本群的任务（最多 10 个）："]
        for record in recent:
            lines.append(
                f"· {record['task_id']} · {record['status']} · {record['agent']} · "
                f"{record['mode']} · {record['prompt'][:24]}"
            )
        lines.append("详情：/task status <任务ID>；取结果：/task result <任务ID>")
        await self._send(message.group_id, "\n".join(lines))

    def _render_task_status(self, record: dict) -> str:
        lines = [
            f"📊 任务 {record['task_id']} · {record['status']}",
            f"Agent：{record['agent']} · 后端：{record['backend'] or '-'} · "
            f"模型：{record['model'] or '-'} · 强度：{record['effort'] or '模型默认'}",
            f"模式：{record['mode']} · 目录：{record['workspace'] or '（无）'}",
            f"任务：{record['prompt'][:120]}",
        ]
        if record["error"]:
            lines.append(f"错误：{record['error']}")
        if record["changed_files"]:
            lines.append(f"文件变更：{record['changed_files']} 个（/task result 查看清单）")
        lines.append(f"耗时：{record['duration_ms'] / 1000:.1f} 秒")
        return "\n".join(lines)

    async def _task_result(self, args: str, message: InboundMessage) -> None:
        task_id = args.strip()
        if not task_id:
            await self._send(message.group_id, "用法：/task result <任务ID>")
            return
        record = self.tasks.own_task(task_id, group_id=message.group_id, user_id=message.user_id)
        if record is None:
            await self._send(message.group_id, NOT_FOUND_TEXT)
            return
        await self._send(message.group_id, self._render_task_status(record))
        if record["result_text"]:
            for part in split_message(
                sanitize_outbound_text(record["result_text"]), self.settings.max_message_length
            ):
                await self._send(message.group_id, part)
        changes = self.tasks._store.list_changes(task_id=task_id)  # noqa: SLF001
        if changes:
            lines = ["🔧 文件变更："]
            for change in changes:
                mark = {"create": "新建", "modify": "修改", "delete": "删除"}.get(
                    change["kind"], change["kind"]
                )
                backup = f"，备份 {Path(change['backup']).name}" if change["backup"] else ""
                error = f"，错误：{change['error']}" if change["error"] else ""
                lines.append(
                    f"· {mark} {Path(change['path']).name} [{change['status']}] "
                    f"{change['before_hash'][:8]}→{change['after_hash'][:8]}{backup}{error}"
                )
            lines.append("恢复：把备份文件复制回原路径（覆盖前请确认目标未被再次修改）。")
            await self._send(message.group_id, sanitize_outbound_text("\n".join(lines)))

    async def _task_cancel(self, args: str, message: InboundMessage) -> None:
        task_id = args.strip()
        if not task_id:
            await self._send(message.group_id, "用法：/task cancel <任务ID>")
            return
        text = await self.tasks.cancel(
            task_id=task_id, group_id=message.group_id, user_id=message.user_id
        )
        await self._send(message.group_id, text)

    # ---------- /workspace ----------

    async def _handle_workspace(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        action = parsed.subcommand
        args = parsed.rest.strip()
        if action == "find":
            keyword = args.strip().strip('"').strip("'")
            if not keyword:
                await self._send(message.group_id, "用法：/workspace find <关键词>")
                return
            try:
                candidates, hint = self.workspaces.find_candidates(
                    group_id=message.group_id, user_id=message.user_id, keyword=keyword
                )
            except WorkspaceError as exc:
                await self._send(message.group_id, str(exc))
                return
            if not candidates:
                await self._send(message.group_id, hint)
                return
            # 独立 find：只绑定目录，不自动创建模型任务
            resolution = self.workspaces.resolve_selector(
                group_id=message.group_id, user_id=message.user_id,
                options=_EmptyOptions(find=keyword), mode="read", purpose="workspace",
            )
            if resolution.awaiting_selection:
                await self._send(message.group_id, sanitize_outbound_text(resolution.message))
            return
        if action == "use":
            raw = args.strip().strip('"').strip("'")
            if not raw:
                await self._send(message.group_id, '用法：/workspace use "/绝对路径"')
                return
            try:
                path, receipt = self.workspaces.bind(
                    group_id=message.group_id, user_id=message.user_id, raw_path=raw
                )
            except WorkspaceError as exc:
                await self._send(message.group_id, f"⛔ 绑定失败：{exc}")
                return
            self._logger.info("workspace bound path=%s user=%d", path, message.user_id)
            await self._send(message.group_id, receipt)
            return
        if action == "status":
            await self._send(
                message.group_id,
                sanitize_outbound_text(
                    self.workspaces.status_text(
                        group_id=message.group_id, user_id=message.user_id
                    )
                ),
            )
            return
        if action == "clear":
            await self._send(
                message.group_id,
                self.workspaces.clear(group_id=message.group_id, user_id=message.user_id),
            )
            return
        if action == "select":
            await self._workspace_select(args, message)
            return
        await self._send(message.group_id, self.commands.help_text("workspace"))

    async def _workspace_select(self, args: str, message: InboundMessage) -> None:
        parts = args.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await self._send(message.group_id, "用法：/workspace select <确认码> <序号>")
            return
        code, index = parts[0], int(parts[1])
        outcome = self.workspaces.select(
            group_id=message.group_id, user_id=message.user_id, code=code, index=index
        )
        if not outcome.ok:
            await self._send(message.group_id, outcome.message)
            return
        await self._send(message.group_id, outcome.message)
        if outcome.resume_task_id:
            await self._resume_task(outcome, message)
        elif outcome.team_id:
            # 团队步骤的目录选定：只写入草稿，由用户显式 /team run 启动
            if self.team is None:
                await self._send(message.group_id, "多 Agent 协作未启用。")
                return
            text = await self.team.on_workspace_selected(outcome, message)
            if text:
                await self._send(message.group_id, text)

    async def _resume_task(self, outcome, message: InboundMessage) -> None:
        """确认目录后只提交一次原任务（确认码一次性，天然防重放）。"""
        result = await self.submission.resume(
            task_id=outcome.resume_task_id, group_id=message.group_id,
            user_id=message.user_id, workspace=outcome.workspace,
        )
        if isinstance(result, str):
            await self._send(message.group_id, result)
            return
        await self._reply_outcome(result, message.group_id)

    # ---------- /agent ----------

    async def _handle_agent(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        action = parsed.subcommand or "status"
        args = parsed.rest.strip()
        if action == "list":
            await self._send(message.group_id, self._agent_list_text())
            return
        if action == "models":
            name = args.split()[0].lower() if args.split() else ""
            await self._send(message.group_id, self._agent_models_text(name))
            return
        if action == "set":
            await self._agent_set(args, message)
            return
        if action == "clear":
            name = args.split()[0].lower() if args.split() else ""
            agent = self.registry.agent(name)
            if agent is None:
                await self._send(
                    message.group_id,
                    "用法：/agent clear <agent>；可选：" + ", ".join(self.registry.all_agent_names()),
                )
                return
            from app.storage.context_store import thread_key

            target = thread_key(name, shared=bool(self.settings.shared_agent_context))
            removed = self.store.clear(
                group_id=message.group_id, user_id=message.user_id, agent=target
            )
            scope = "共享线程（所有 Agent）" if target != name else f"本群 × 你 × {name}"
            await self._send(
                message.group_id,
                f"🧹 已清除 {name} 的上下文（{scope}，共删除 {removed} 条消息）。",
            )
            return
        # status [agent]
        name = args.split()[0].lower() if args.split() else ""
        await self._send(message.group_id, self._agent_status_text(name, message))

    def _agent_list_text(self) -> str:
        lines = ["🤖 逻辑 Agent 与后端："]
        for agent in self.registry.agent_discovery():
            state = "✅" if agent["configured"] else "❌"
            caps = "/".join(sorted(agent["capabilities"])) or "text"
            lines.append(
                f"{state} {agent['name']}（{agent['display_name']}）默认后端 {agent['default_backend']} · "
                f"能力 {caps}"
            )
            for backend in self.registry.for_agent(agent["name"]):
                descriptor = backend.descriptor
                mark = "⏸" if not descriptor.enabled else ("✅" if descriptor.configured else "❌")
                reason = "" if descriptor.configured else f" — {descriptor.unavailable_reason}"
                lines.append(
                    f"    {mark} {descriptor.id}（{descriptor.adapter_type}，隔离 {descriptor.isolation}）"
                    f" 原生 {'/'.join(sorted(descriptor.native_modes)) or '无'}"
                    f" · broker {'/'.join(sorted(descriptor.broker_modes)) or '无'}{reason}"
                )
        lines.append("查看模型：/agent models <agent>；保存默认：/agent set <agent> --model ...")
        return "\n".join(lines)

    def _agent_models_text(self, name: str) -> str:
        agent = self.registry.agent(name)
        if agent is None:
            return (
                "用法：/agent models <agent>；可选："
                + ", ".join(self.registry.all_agent_names())
            )
        lines = [f"🧠 {name}（{agent.display_name}）的可用模型 / 强度（来自真实后端目录）："]
        any_model = False
        for backend in self.registry.for_agent(name):
            descriptor = backend.descriptor
            state = "✅" if descriptor.configured else "❌ " + (descriptor.unavailable_reason or "未配置")
            lines.append(f"· 后端 {descriptor.id}：{state}")
            for model, efforts in descriptor.model_efforts.items():
                any_model = True
                lines.append(
                    f"    {model}：{', '.join(efforts) if efforts else '仅模型默认'}"
                )
            if not descriptor.model_efforts:
                lines.append("    （该后端未报告模型目录）")
        if not any_model:
            lines.append("暂无可选模型；请检查后端配置。")
        return "\n".join(lines)

    async def _agent_set(self, args: str, message: InboundMessage) -> None:
        parts = args.split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if self.registry.agent(name) is None:
            await self._send(
                message.group_id,
                "用法：/agent set <agent> [--model 模型] [--effort 强度|default] [--backend ID]；"
                "可选：" + ", ".join(self.registry.all_agent_names()),
            )
            return
        spec = self.commands.get("agent")
        try:
            options = parse_public_options(rest, spec=spec)
        except OptionError as exc:
            await self._send(message.group_id, str(exc))
            return
        if options.directory is not None or options.find is not None or options.mode is not None:
            await self._send(
                message.group_id,
                "目录与模式不能保存为默认；请在 /task 中显式使用 --dir/--find/--mode。",
            )
            return
        if options.body:
            await self._send(
                message.group_id,
                "用法：/agent set <agent> [--model 模型] [--effort 强度|default] [--backend ID]",
            )
            return
        choice, error = self.tasks.resolve_selection(
            agent_name=name, group_id=message.group_id, user_id=message.user_id,
            model=options.model,
            effort=None if options.effort == "default" else options.effort,
            effort_set=options.effort_set, backend_id=options.backend,
        )
        if choice is None:
            await self._send(message.group_id, error)
            return
        backend_id, model, effort = choice
        if options.given:
            from app.models import InvocationOptions

            self.store.set_preferences(
                group_id=message.group_id, user_id=message.user_id, agent=name,
                options=InvocationOptions(model=model, effort=effort, backend=backend_id),
            )
        display = self.registry.agent(name).display_name
        await self._send(
            message.group_id,
            f"[{display}] {'已保存默认' if options.given else '当前默认'}："
            f"后端 {backend_id} · 模型 {model} · 强度 {effort or '模型默认'}（本群 × 你）",
        )

    def _agent_status_text(self, name: str, message: InboundMessage | None) -> str:
        lines = ["🤖 Agent 状态："]
        targets = [name] if name else self.registry.all_agent_names()
        for agent_name in targets:
            agent = self.registry.agent(agent_name)
            if agent is None:
                lines.append(f"  · 未知 Agent：{agent_name}")
                continue
            backend = self.registry.get(agent.default_backend)
            if backend is None:
                state = "❌ 无后端"
            elif not backend.descriptor.enabled:
                state = "⏸ 已禁用"
            elif backend.descriptor.configured:
                state = f"✅ 已配置（{backend.descriptor.default_model or backend.adapter.model_name}）"
            else:
                state = f"❌ {backend.descriptor.unavailable_reason or '未配置'}"
            lines.append(f"  · {agent.display_name}（/task {agent.name}）：{state}")
            if message is not None:
                saved = self.store.preferences(
                    group_id=message.group_id, user_id=message.user_id, agent=agent.name
                )
                lines.append(
                    f"    你的默认：{saved.model if saved else agent.default_model or '不可用'}"
                    f" / {saved.effort if saved and saved.effort else '模型默认'}"
                )
        bound = (
            self.workspaces.binding(group_id=message.group_id, user_id=message.user_id)
            if message is not None else None
        )
        lines.append(f"绑定目录：{bound or '（无）'}")
        lines.append(
            f"上下文：最近 {self.settings.max_context_turns} 轮 · "
            f"{'共享线程（各 Agent 互相可见）' if self.settings.shared_agent_context else '各 Agent 隔离'} · "
            f"限流：{self.settings.rate_limit_per_minute} 次/分钟"
        )
        lines.append(
            f"目录锁：{len(self.workspaces._coordinator.active_locks())} 个占用 · "  # noqa: SLF001
            f"worker 上限 {self.workspaces._coordinator.max_workers}"  # noqa: SLF001
        )
        lines.append(f"待确认操作：{self.confirmations.pending_count()} 个")
        return "\n".join(lines)

    # ---------- /team ----------

    async def _handle_team(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        if self.team is None or not self.settings.team_enabled:
            await self._send(message.group_id, "多 Agent 协作未启用。")
            return
        await self.team.handle(parsed, message)

    # ---------- /confirm ----------

    async def _handle_confirm(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        code = parsed.rest.strip().upper()
        if not code:
            await self._send(message.group_id, "用法：/confirm <确认码>")
            return
        pending = self.confirmations.peek(code)
        if pending is None:
            await self._send(message.group_id, "❌ 确认码无效或已被使用。")
            return
        if pending.kind == ConfirmationKind.WORKSPACE_SELECT:
            await self._send(
                message.group_id,
                f"该确认码用于目录选择，请发送：/workspace select {code} <序号>",
            )
            return
        outcome, record = self.confirmations.consume(
            kind=pending.kind, user_id=message.user_id, code=code, group_id=message.group_id
        )
        if outcome is not ConfirmationOutcome.OK or record is None:
            await self._send(message.group_id, self._confirm_error(outcome))
            return
        if record.kind == ConfirmationKind.ACTION and record.action is not None:
            text = await self._handle_team_confirmation(record, message)
            if text:
                await self._send(message.group_id, text)
                return
        await self._send(
            message.group_id,
            f"✅ 已确认：{record.summary}（该确认没有需要额外执行的动作）。",
        )

    async def _handle_team_confirmation(self, record, message: InboundMessage) -> str:
        """team 相关的确认动作（例如确认重跑已产生副作用的步骤）。"""
        if self.team is None:
            return ""
        action = record.action
        if action is None or action.kind != "team.retry":
            return ""
        return await self.team.confirm_action(record, message)

    @staticmethod
    def _confirm_error(outcome: ConfirmationOutcome) -> str:
        return {
            ConfirmationOutcome.NOT_FOUND: "❌ 确认码无效或已被使用。",
            ConfirmationOutcome.EXPIRED: "❌ 确认码已过期，请重新发起操作。",
            ConfirmationOutcome.WRONG_USER: "❌ 该确认码不属于你。",
            ConfirmationOutcome.WRONG_SCOPE: "❌ 该确认码是在别的群发起的。",
            ConfirmationOutcome.WRONG_KIND: "❌ 确认码类型不匹配。",
        }.get(outcome, "❌ 确认失败。")

    # ---------- /access ----------

    async def _handle_access(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        action = parsed.subcommand
        parts = parsed.rest.split()
        usage = "用法：/access add <QQ号>、/access remove <QQ号>、/access list；授权仅在当前群生效。"
        if action == "list":
            users = sorted(self.authz.members(message.group_id) - self._ignore_users - {message.self_id})
            rows = [
                f"{user}（管理员）" if self.authz.admin_allowed(user) else str(user)
                for user in users
            ]
            await self._send(message.group_id, "本群授权名单：\n" + "\n".join(rows))
            return
        if action not in {"add", "remove"} or len(parts) != 1 or not parts[0].isdigit():
            await self._send(message.group_id, usage)
            return
        target = int(parts[0])
        if not 0 < target < 2**63:
            await self._send(message.group_id, "QQ号格式无效。")
            return
        if self.authz.admin_allowed(target):
            await self._send(message.group_id, "该用户是配置中的管理员，不能通过群命令修改其权限。")
            return
        if target in self._ignore_users or target == message.self_id:
            await self._send(message.group_id, "不能授权机器人或忽略名单中的用户。")
            return
        allowed = action == "add"
        self.store.set_whitelist(message.group_id, target, allowed, message.user_id)
        self._logger.info(
            "access action=%s group=%d actor=%d target=%d",
            action, message.group_id, message.user_id, target,
        )
        revoked = 0
        if not allowed:
            if self.team is not None:
                revoked = await self.team.revoke_access(message.group_id, target)
            cancelled = await self.tasks.cancel_all_for(
                group_id=message.group_id, user_id=target
            )
            self._logger.info(
                "撤权后取消任务：team=%d task=%d group=%d target=%d",
                revoked, cancelled, message.group_id, target,
            )
        await self._send(
            message.group_id,
            f"已{'授权' if allowed else '撤销'} {target} 在本群使用机器人。"
            + ("可聊天与选择模型；目录读写/执行需管理员配置目录 ACL。" if allowed
               else (f"已同时取消其 {revoked + cancelled} 个活动任务。" if revoked + cancelled else "")),
        )

    # ---------- /command、/help ----------

    async def _handle_command(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        if parsed.subcommand == "show":
            name = parsed.rest.strip().lower().lstrip("/")
            if not name:
                await self._send(message.group_id, "用法：/command show <名称>")
                return
            if name in self.presets:
                preset = self.presets[name]
                await self._send(
                    message.group_id,
                    f"📖 /{name}（preset）— {preset.description}\n"
                    f"固定 Agent：{preset.agent} · 固定模式：{preset.mode}\n"
                    f"模板：{preset.template}\n"
                    f"可覆盖：{', '.join(preset.allow_overrides) or '（无）'}\n"
                    f"用法：/{name} [--dir \"路径\"|--find \"关键词\"] -- <正文>",
                )
                return
            await self._send(message.group_id, self.commands.help_text(name))
            return
        lines = ["📖 已登记命令（不含旧口令）："]
        for spec in self.commands.all_specs():
            lines.append(f"/{spec.name} — {spec.description}")
        if self.presets:
            lines.append("自定义 preset：")
            for name, preset in self.presets.items():
                lines.append(f"/{name} — {preset.description}（固定 Agent {preset.agent}）")
        lines.append("参数细节：/command show <名称>；按族帮助：/help <命令族>")
        await self._send(message.group_id, "\n".join(lines))

    async def _handle_help(self, parsed: ParsedCommand, message: InboundMessage) -> None:
        await self._send(message.group_id, self.commands.help_text(parsed.rest))

    # ---------- 回复 ----------

    async def _reply_outcome(self, outcome: TaskOutcome, group_id: int) -> None:
        header = f"[{outcome.display} · task {outcome.task_id}]"
        header += f" 后端：{outcome.backend or '-'}"
        if outcome.model:
            header += f" · 模型：{outcome.model} · 强度：{outcome.effort or '模型默认'}"
        header += f" · 模式：{outcome.mode}"
        if not outcome.ok:
            text = outcome.detail or outcome.error_text
            if outcome.error == "invalid_option" or outcome.error == "capability_not_supported":
                text = outcome.detail or outcome.error_text
            await self._send(group_id, f"{header} 失败：{sanitize_outbound_text(text)}")
            if outcome.changes:
                await self._send(
                    group_id,
                    sanitize_outbound_text(self._changes_text(outcome, partial=True)),
                )
            return
        # V01：会话完成但没有任何实际操作 → 明确标注"未修改"，绝不显示为已完成修改
        if outcome.status == "no_change":
            header += "（会话完成，无实际操作）"
        if outcome.thinking and self.settings.max_thinking_chars > 0:
            await self._send_thinking(group_id, header, outcome.thinking)
        body = sanitize_outbound_text(outcome.content)
        if outcome.status == "no_change":
            body = (
                "⚠️ 本次没有记录到任何实际执行的操作（文件未修改、命令未执行）。\n"
                "如果你确实要求了修改，请重试或换用更强的模型。\n\n" + body
            )
        if outcome.tool_log:
            body += "\n\n🔧 操作记录：\n" + "\n".join(
                f"· {line}" for line in outcome.tool_log[:20]
            )
        if outcome.changes:
            body += "\n\n" + self._changes_text(outcome, partial=outcome.partial)
        parts = split_message(body, self.settings.max_message_length)
        total = len(parts)
        for index, part in enumerate(parts, start=1):
            suffix = f" ({index}/{total})" if total > 1 else ""
            await self._send(group_id, f"{header}{suffix}\n{part}")

    def _changes_text(self, outcome: TaskOutcome, *, partial: bool) -> str:
        lines = ["🔧 文件变更：" + ("（含未完成项）" if partial else "")]
        for change in outcome.changes[:12]:
            mark = {"create": "新建", "modify": "修改", "delete": "删除"}.get(
                change.kind, change.kind
            )
            state = "已应用" if change.status == "applied" else f"{change.status}：{change.error}"
            backup = f"，备份 {Path(change.backup).name}" if change.backup else ""
            lines.append(
                f"· {mark} {Path(change.path).name} [{state}] "
                f"{change.before_hash[:8]}→{change.after_hash[:8]}{backup}"
            )
        if len(outcome.changes) > 12:
            lines.append(f"…… 还有 {len(outcome.changes) - 12} 个文件未列出")
        if partial:
            lines.append("⚠️ 部分文件未能写入；请按 /task result 的清单逐个核对，恢复请使用备份。")
        return "\n".join(lines)

    async def _send_thinking(self, group_id: int, header: str, thinking: str) -> None:
        budget = self.settings.max_thinking_chars
        text = (thinking or "").strip()
        if not text or budget <= 0:
            return
        preview = text if len(text) <= budget else text[:budget].rstrip() + (
            f"……（思考过程共 {len(text)} 字，已截断）"
        )
        for part in split_message(sanitize_outbound_text(preview), self.settings.max_message_length):
            await self._send(group_id, f"{header} 📝 推理摘要\n{part}")

    async def _send(self, group_id: int, text: str) -> None:
        safe_text = self.redactor.redact(text)
        try:
            await self.transport.send_group_message(group_id, safe_text)
        except TransportError:
            self._logger.exception("消息发送失败 group=%d", group_id)


class _EmptyOptions:
    """给 WorkspaceService 用的空选择器（find 从关键词重建，不使用这里的值）。"""

    find: str | None = None
    directory: str | None = None
    mode: str | None = None

    def __init__(self, find: str | None = None) -> None:
        self.find = find


def _preset_spec(preset: PresetSpec, base_spec):
    """为 preset 生成选项规格：只保留它允许覆盖的选项。"""
    from app.commands.spec import CommandSpec

    allowed = [
        option for option in base_spec.options if option.name in set(preset.allow_overrides)
    ]
    return CommandSpec(
        name=preset.name,
        description=preset.description,
        usage=f"/{preset.name} [选项] -- <正文>",
        handler_key="task",
        options=tuple(allowed),
    )


def _options_from_preset(expanded):
    from app.commands.options import PublicOptions

    return PublicOptions(
        body=expanded.body, model=expanded.model, effort=expanded.effort,
        effort_set=expanded.effort_set, backend=expanded.backend, mode=expanded.mode,
        directory=expanded.directory, find=expanded.find,
    )
