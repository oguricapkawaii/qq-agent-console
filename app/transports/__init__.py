"""传输层：QQ 接入抽象与 OneBot v11 实现。

Transport 接口与具体协议解耦——未来可增加官方机器人 API、其它 IM 协议等实现，
而不需要改动路由 / Agent / 存储层。
"""

from app.transports.base import InboundEventHandler, Transport, TransportError, build_transport

__all__ = ["InboundEventHandler", "Transport", "TransportError", "build_transport"]
