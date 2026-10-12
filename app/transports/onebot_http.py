"""OneBot v11 HTTP 传输。

- OneBot 实现把事件 POST 到本服务的 /onebot/event（支持 access_token 鉴权）；
- 本服务通过 OneBot 实现的 HTTP API（/send_group_msg）发送消息。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.transports.base import InboundEventHandler, Transport, TransportError

logger = logging.getLogger("qqbot.transport.http")


class OneBotHTTPTransport(Transport):
    def __init__(
        self,
        *,
        api_url: str,
        access_token: str = "",
        on_event: InboundEventHandler,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = (api_url or "").rstrip("/")
        self._token = access_token or ""
        self._on_event = on_event
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        self._client = httpx.AsyncClient(
            base_url=self._api_url, headers=headers, transport=http_transport
        )
        self._event_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        logger.info("OneBot HTTP 传输就绪（API：%s），等待事件上报…", self._api_url)

    async def stop(self) -> None:
        for task in list(self._event_tasks):
            task.cancel()
        await self._client.aclose()

    def install(self, app: FastAPI) -> None:
        """把事件上报路由挂到 FastAPI 应用上。"""

        @app.post("/onebot/event")
        async def onebot_event(request: Request) -> dict:
            if not self._authorized(request):
                raise HTTPException(status_code=401, detail="unauthorized")
            try:
                payload = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid json") from None
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="invalid event")
            # 快速返回 200，事件在后台任务中处理
            task = asyncio.create_task(self._safe_dispatch(payload))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)
            return {"status": "accepted"}

    def _authorized(self, request: Request) -> bool:
        if not self._token:
            return True
        provided = request.headers.get("Authorization", "")
        if provided.startswith("Bearer ") and self._token_eq(provided[len("Bearer "):].strip()):
            return True
        if self._token_eq(request.query_params.get("access_token", "")):
            return True
        return False

    def _token_eq(self, provided: str) -> bool:
        try:
            return hmac.compare_digest(provided.encode(), self._token.encode())
        except Exception:
            return False

    async def _safe_dispatch(self, payload: dict) -> None:
        try:
            await self._on_event(payload)
        except Exception:
            logger.exception("处理 OneBot 事件失败")

    async def send_group_message(self, group_id: int, text: str) -> None:
        payload: dict[str, Any] = {"group_id": group_id, "message": text}
        try:
            response = await self._client.post("/send_group_msg", json=payload, timeout=10.0)
        except httpx.HTTPError as exc:
            raise TransportError(f"调用 OneBot HTTP API 失败：{type(exc).__name__}") from exc
        if response.status_code != 200:
            raise TransportError(f"OneBot HTTP API 返回 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise TransportError("OneBot HTTP API 响应解析失败") from None
        if data.get("status") != "ok":
            reason = data.get("wording") or data.get("msg") or "unknown"
            raise TransportError(f"OneBot 拒绝了消息发送：{reason}")
