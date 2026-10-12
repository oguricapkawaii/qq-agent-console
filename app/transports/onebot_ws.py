"""OneBot v11 正向 WebSocket 传输。

本程序作为 WebSocket 客户端连接 OneBot 实现（NapCat / Lagrange / LLOneBot 等）
提供的 WS 服务端：事件从服务端推下来，动作（发消息）从同一连接发上去。
断线自动指数退避重连。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

import websockets

from app.transports.base import InboundEventHandler, Transport, TransportError

logger = logging.getLogger("qqbot.transport.ws")


class OneBotWebSocketTransport(Transport):
    def __init__(self, *, ws_url: str, access_token: str = "", on_event: InboundEventHandler) -> None:
        self._bot_account: int | None = None
        self._url = ws_url
        self._token = access_token
        self._on_event = on_event
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._connected = asyncio.Event()
        self._event_tasks: set[asyncio.Task] = set()

    def bot_account(self) -> int | None:
        """机器人自身账号：由入站事件（OneBot `self_id`）记录，登录前后都可用。"""
        return self._bot_account

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="onebot-ws-client")

    async def stop(self) -> None:
        self._stopping = True
        self._connected.set()  # 唤醒可能在等待连接的发送方
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        for task in list(self._event_tasks):
            task.cancel()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                async with self._connect() as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = 1.0
                    logger.info("已连接 OneBot WebSocket：%s", self._url)
                    async for raw in ws:
                        self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    break
                logger.warning(
                    "OneBot WebSocket 断开（%s），%.0f 秒后重连", type(exc).__name__, backoff
                )
            finally:
                self._ws = None
                self._connected.clear()
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _connect(self) -> Any:
        headers = [("Authorization", f"Bearer {self._token}")] if self._token else []
        try:
            return websockets.connect(
                self._url, additional_headers=headers, open_timeout=10, ping_interval=20
            )
        except TypeError:
            # 兼容旧版 websockets（参数名不同）
            return websockets.connect(
                self._url, extra_headers=headers, open_timeout=10, ping_interval=20
            )

    def _handle_raw(self, raw: Any) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            logger.warning("收到无法解析的 WebSocket 消息")
            return
        if not isinstance(event, dict):
            return
        # 注意：这里不把事件身份写进共享字段。去重键必须用**每条消息自带**的
        # `self_id`（见 app/routing/qq_dedup.build_key），否则两个账号的事件交错时，
        # 排队中的旧事件会被"最后收到的账号"改写键（F5）。
        task = asyncio.create_task(self._safe_dispatch(event))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def _safe_dispatch(self, event: dict) -> None:
        try:
            await self._on_event(event)
        except Exception:
            logger.exception("处理 OneBot 事件失败")

    async def send_group_message(self, group_id: int, text: str) -> None:
        payload = {
            "action": "send_group_msg",
            "params": {"group_id": group_id, "message": text},
            "echo": uuid.uuid4().hex[:8],
        }
        if self._ws is None:
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                raise TransportError("OneBot WebSocket 未连接，消息发送失败") from None
        ws = self._ws
        if ws is None:
            raise TransportError("OneBot WebSocket 未连接，消息发送失败")
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            raise TransportError(f"WebSocket 发送失败：{type(exc).__name__}") from exc
