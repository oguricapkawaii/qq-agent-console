"""OneBot HTTP 传输测试：事件上报鉴权 + API 发送。"""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI

from app.transports.onebot_http import OneBotHTTPTransport

TOKEN = "test-token-123456"

EVENT = {
    "post_type": "message",
    "message_type": "group",
    "group_id": 200,
    "user_id": 100,
    "self_id": 999,
    "message": "/task glm -- 你好",
    "raw_message": "/task glm -- 你好",
}


def make_transport(events: list[dict]) -> OneBotHTTPTransport:
    async def on_event(event: dict) -> None:
        events.append(event)

    return OneBotHTTPTransport(
        api_url="http://onebot.test", access_token=TOKEN, on_event=on_event
    )


async def test_send_group_message_calls_onebot_api():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://onebot.test/send_group_msg"
        assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 1}})

    async def on_event(event):  # pragma: no cover
        pass

    transport = OneBotHTTPTransport(
        api_url="http://onebot.test", access_token=TOKEN, on_event=on_event,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        await transport.send_group_message(200, "hello")
        assert sent == [{"group_id": 200, "message": "hello"}]
    finally:
        await transport.stop()


async def test_event_endpoint_rejects_missing_token():
    events: list[dict] = []
    transport = make_transport(events)
    app = FastAPI()
    transport.install(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/onebot/event", json=EVENT)
        assert response.status_code == 401
        await asyncio.sleep(0.05)
        assert events == []


async def test_event_endpoint_accepts_bearer_token_and_dispatches():
    events: list[dict] = []
    transport = make_transport(events)
    app = FastAPI()
    transport.install(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/onebot/event", json=EVENT, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        # query 参数形式的 token 也可以
        response = await client.post(
            f"/onebot/event?access_token={TOKEN}", json=EVENT
        )
        assert response.status_code == 200
        # 错误的 token 被拒
        response = await client.post(
            "/onebot/event", json=EVENT, headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code == 401

    await asyncio.sleep(0.05)
    assert len(events) == 2
    assert events[0]["raw_message"] == "/task glm -- 你好"


async def test_event_endpoint_rejects_invalid_payload():
    events: list[dict] = []
    transport = make_transport(events)
    app = FastAPI()
    transport.install(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/onebot/event", content=b"not-json", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 400
        response = await client.post(
            "/onebot/event", json=[1, 2, 3], headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 400
    assert events == []
