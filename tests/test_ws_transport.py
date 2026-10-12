"""OneBot WebSocket 传输集成测试（本地起一个假 OneBot WS 服务端）。"""

from __future__ import annotations

import asyncio
import json
import socket

import websockets

from app.transports.onebot_ws import OneBotWebSocketTransport


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def test_ws_transport_receives_events_and_sends_actions():
    events: list[dict] = []
    actions: asyncio.Queue = asyncio.Queue()

    async def onebot_server(ws, path=None):
        # 模拟 OneBot 实现推送一条群消息事件
        await ws.send(json.dumps({
            "post_type": "message",
            "message_type": "group",
            "group_id": 200,
            "user_id": 100,
            "self_id": 999,
            "message": "/task glm -- 你好",
            "raw_message": "/task glm -- 你好",
            "sender": {"nickname": "u"},
        }))
        try:
            while True:
                raw = await ws.recv()
                await actions.put(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    port = free_port()
    async with websockets.serve(onebot_server, "127.0.0.1", port):
        async def on_event(event: dict) -> None:
            events.append(event)

        transport = OneBotWebSocketTransport(
            ws_url=f"ws://127.0.0.1:{port}", access_token="", on_event=on_event
        )
        await transport.start()
        try:
            assert await wait_until(lambda: bool(events)), "未收到事件"
            assert events[0]["raw_message"] == "/task glm -- 你好"

            await transport.send_group_message(200, "回复内容")
            action = await asyncio.wait_for(actions.get(), timeout=3)
            assert action["action"] == "send_group_msg"
            assert action["params"]["group_id"] == 200
            assert action["params"]["message"] == "回复内容"
        finally:
            await transport.stop()


async def test_ws_transport_reconnects_after_disconnect():
    """服务端主动断开后，客户端应自动重连。"""
    port = free_port()

    async def drop_immediately(ws, path=None):
        await ws.close()

    async def stay_alive(ws, path=None):
        await asyncio.sleep(10)

    async def on_event(event: dict) -> None:  # pragma: no cover - 本测试不关注事件
        pass

    transport = OneBotWebSocketTransport(
        ws_url=f"ws://127.0.0.1:{port}", access_token="", on_event=on_event
    )

    # 第一个服务端：接受连接后立刻断开
    server1 = await websockets.serve(drop_immediately, "127.0.0.1", port)
    await transport.start()
    try:
        # 等第一次连接建立并断开
        await asyncio.sleep(0.3)
        server1.close()
        await server1.wait_closed()

        # 第二个服务端：保持连接
        server2 = await websockets.serve(stay_alive, "127.0.0.1", port)
        try:
            # 退避 1 秒后应重连成功
            assert await wait_until(lambda: transport._ws is not None, timeout=5), "未能自动重连"
        finally:
            server2.close()
            await server2.wait_closed()
    finally:
        await transport.stop()
