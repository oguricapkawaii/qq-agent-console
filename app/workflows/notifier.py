"""协作通知：统一脱敏、清洗、分段后发送；发送失败不影响任务状态。"""

from __future__ import annotations

import logging

from app.config import Settings
from app.security.authz import Authorizer
from app.security.redaction import SecretRedactor
from app.security.text_safety import sanitize_outbound_text, split_message
from app.transports.base import Transport, TransportError

logger = logging.getLogger("qqbot.workflows.notifier")


class Notifier:
    def __init__(
        self,
        *,
        transport: Transport,
        authz: Authorizer,
        redactor: SecretRedactor,
        settings: Settings,
    ) -> None:
        self._transport = transport
        self._authz = authz
        self._redactor = redactor
        self._settings = settings

    async def notify(self, group_id: int, owner_user_id: int, text: str) -> bool:
        """发送前复查群权限与发起者授权；撤销后不再推送。"""
        if not self._authz.group_allowed(group_id):
            logger.info("group=%d 已不在允许名单，跳过通知", group_id)
            return False
        if not self._authz.user_allowed(owner_user_id, group_id):
            logger.info("user=%d 授权已撤销，跳过通知", owner_user_id)
            return False
        safe = sanitize_outbound_text(self._redactor.redact(text or ""))
        if not safe.strip():
            return False
        sent = False
        for part in split_message(safe, self._settings.max_message_length):
            try:
                await self._transport.send_group_message(group_id, part)
                sent = True
            except TransportError:
                logger.exception("协作通知发送失败 group=%d", group_id)
            except Exception:
                # 结果推送失败不重新运行步骤，DB 成功结果仍可通过 /team result 取回
                logger.exception("协作通知异常 group=%d", group_id)
        return sent
