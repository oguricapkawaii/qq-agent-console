"""Transport 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from app.config import Settings

# 传输层收到原始事件（如 OneBot v11 的 JSON dict）后交给调度器的回调
InboundEventHandler = Callable[[dict], Awaitable[None]]


class TransportError(RuntimeError):
    """消息发送等传输操作失败。"""


class Transport(ABC):
    @abstractmethod
    async def start(self) -> None:
        """启动传输（连接、监听等）。"""

    @abstractmethod
    async def stop(self) -> None:
        """停止传输并释放资源。"""

    @abstractmethod
    async def send_group_message(self, group_id: int, text: str) -> None:
        """发送一条群文本消息。超长文本应由上层预先分段。"""

    def bot_account(self) -> int | None:
        """本机器人自己的 QQ 账号（用于消息去重命名空间）；未知时返回 None。

        默认实现返回 None：不额外发起 OneBot 请求，避免为去重引入网络往返与失败面。
        具体传输在收到入站事件时从 `self_id` 记录真实账号。
        """
        return None


def build_transport(settings: Settings, on_event: InboundEventHandler) -> Transport:
    """根据配置选择传输实现。"""
    # 延迟导入避免包内循环依赖
    from app.transports.onebot_http import OneBotHTTPTransport
    from app.transports.onebot_ws import OneBotWebSocketTransport

    kind = settings.transport.strip().lower()
    if kind == "onebot_ws":
        return OneBotWebSocketTransport(
            ws_url=settings.onebot_ws_url,
            access_token=settings.onebot_access_token,
            on_event=on_event,
        )
    if kind == "onebot_http":
        return OneBotHTTPTransport(
            api_url=settings.onebot_http_api_url,
            access_token=settings.onebot_access_token,
            on_event=on_event,
        )
    raise ValueError(f"未知的 transport 类型：{settings.transport}（可选：onebot_ws / onebot_http）")
