"""OneBot v11 事件解析（WS 与 HTTP 两种传输共用）。

只关心群文本消息；其他事件（私聊、通知、请求、自身消息回显等）返回 None。
消息段（数组形式）只提取 text 部分——图片、@、引用等内容属于不可信输入，
MVP 不做富媒体还原（引用消息原文需要额外调用 get_msg 获取，未来再接入）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.models import InboundMessage
from app.security.text_safety import sanitize_inbound_text


def parse_onebot_group_message(event: Mapping[str, Any]) -> InboundMessage | None:
    """把 OneBot 事件解析为群文本消息；不属于群文本消息则返回 None。"""
    if not isinstance(event, Mapping):
        return None
    # message_sent 是机器人自身发出消息的回显，必须忽略（防循环）
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "group":
        return None
    try:
        group_id = int(event["group_id"])
        user_id = int(event["user_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if group_id <= 0 or user_id <= 0:
        return None
    self_id = event.get("self_id") or 0
    try:
        self_id = int(self_id)
    except (TypeError, ValueError):
        self_id = 0
    sender = event.get("sender") if isinstance(event.get("sender"), Mapping) else {}
    return InboundMessage(
        group_id=group_id,
        user_id=user_id,
        self_id=self_id,
        text=_extract_text(event.get("message"), event.get("raw_message")),
        message_id=event.get("message_id"),
        nickname=sender.get("nickname"),
    )


def _extract_text(message: Any, raw_message: Any) -> str:
    """从字符串形式或消息段数组形式中提取纯文本（并消毒）。"""
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if isinstance(segment, Mapping) and segment.get("type") == "text":
                data = segment.get("data")
                if isinstance(data, Mapping) and isinstance(data.get("text"), str):
                    parts.append(data["text"])
        return sanitize_inbound_text("".join(parts))
    raw = message if isinstance(message, str) else raw_message
    return sanitize_inbound_text(str(raw) if raw is not None else "")
