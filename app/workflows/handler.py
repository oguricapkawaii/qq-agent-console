"""`/team` 命令处理器：解析 -> 服务 -> 统一回执。

只做解析与转发；权限前置在 Dispatcher，逐步授权复查在 executor/notifier。
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.models import InboundMessage
from app.workflows.notifier import Notifier
from app.workflows.parser import parse_team_command
from app.workflows.service import WorkflowService

logger = logging.getLogger("qqbot.workflows.handler")


class TeamCommandHandler:
    def __init__(
        self,
        *,
        service: WorkflowService,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self._service = service
        self._notifier = notifier
        self._settings = settings

    async def handle(self, command, message: InboundMessage) -> None:
        """command 是注册表解析出的 ParsedCommand（args = /team 之后的原文）。"""
        if not self._settings.team_enabled:
            await self._reply(message, "多 Agent 协作未启用。")
            return
        args = getattr(command, "args", "") or ""
        try:
            parsed = parse_team_command(
                args,
                max_prompt_chars=self._settings.team_max_prompt_chars,
                max_steps=self._settings.team_max_steps,
            )
        except ValueError as exc:
            await self._reply(message, str(exc))
            return
        try:
            text = await self._service.execute(parsed, message)
        except Exception:
            logger.exception("team 命令处理异常 user=%d group=%d", message.user_id, message.group_id)
            text = "⛔ 团队命令处理失败（内部错误，详情见服务端日志）。"
        await self._reply(message, text)

    async def _reply(self, message: InboundMessage, text: str) -> None:
        await self._notifier.notify(message.group_id, message.user_id, text)

    async def handle_text(self, command, message) -> str:
        """执行一条 /team 命令并**返回**文本（HTTP API 复用，不直接发群消息）。"""
        if not self._settings.team_enabled:
            return "多 Agent 协作未启用。"
        parsed = parse_team_command(
            getattr(command, "args", "") or "",
            max_prompt_chars=self._settings.team_max_prompt_chars,
            max_steps=self._settings.team_max_steps,
        )
        return await self._service.execute(parsed, message)

    async def confirm_action(self, record, message) -> str:
        return await self._service.handle_confirmation(record, message)

    async def revoke_access(self, group_id: int, user_id: int) -> int:
        """授权撤销回调：取消该用户在本群的活动协作任务。"""
        return await self._service.revoke_access(group_id, user_id)

    async def on_workspace_selected(self, outcome, message: InboundMessage) -> str:
        """目录选择确认后的团队步骤回调。"""
        return await self._service.on_workspace_selected(outcome, message)
