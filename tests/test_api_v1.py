"""A18/A19：统一 HTTP API（复用端口、鉴权、幂等、归属隔离）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import build_identity_table, create_router
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    OTHER_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)

TOKEN = "test-token-aaa"
OTHER_TOKEN = "test-token-bbb"


def build_api_app(harness: ConsoleHarness, tokens: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.state.dispatcher = harness.dispatcher
    app.state.tasks = harness.tasks
    app.state.workspaces = harness.workspaces
    app.state.background = set()
    app.state.api_tokens = _default_tokens() if tokens is None else tokens
    return _mount(app, tokens)


def _default_tokens() -> dict:
    from app.api.v1 import ApiIdentity

    return {
        TOKEN: ApiIdentity(
            name="tester", user_id=ALLOWED_USER, group_ids=(ALLOWED_GROUP,),
            scopes=frozenset({"task", "team", "workspace"}),
        ),
        OTHER_TOKEN: ApiIdentity(
            name="other", user_id=OTHER_USER, group_ids=(ALLOWED_GROUP,),
            scopes=frozenset({"task", "team", "workspace"}),
        ),
    }


def _mount(app: FastAPI, tokens: dict | None) -> FastAPI:
    if tokens is not None:
        app.state.api_tokens = tokens
    app.include_router(create_router(lambda target: target.state))
    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _drain(app: FastAPI) -> None:
    """等待 API 提交的后台任务收敛，避免测试结束时连接已关闭。"""
    import asyncio

    jobs = list(getattr(app.state, "background", set()))
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)


# ---------- 鉴权 ----------


async def test_api_requires_token(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            assert (await http.get("/api/v1/agents")).status_code == 401
            assert (await http.get("/api/v1/agents", headers=auth("nope"))).status_code == 401
            ok = await http.get("/api/v1/agents", headers=auth())
            assert ok.status_code == 200
            body = ok.json()
            assert any(item["name"] == "deepseek" for item in body["agents"])
    finally:
        await harness.aclose()


async def test_disabled_api_returns_503(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = build_api_app(harness, tokens={})
    try:
        async with client(app) as http:
            assert (await http.get("/api/v1/commands", headers=auth())).status_code == 503
    finally:
        await harness.aclose()


def test_token_table_requires_credentials() -> None:
    with pytest.raises(ValueError):
        build_identity_table({"tokens": [{"user_id": 1}]})
    with pytest.raises(ValueError):
        build_identity_table({"tokens": [{"token_env": "NOT_SET_ANYWHERE"}]})
    table = build_identity_table({
        "tokens": [{"token": "x", "user_id": 7, "group_ids": [9], "scopes": ["task"]}]
    })
    assert table["x"].user_id == 7
    assert table["x"].group(9) == 9
    with pytest.raises(Exception):
        table["x"].group(12345)


# ---------- 任务 ----------


async def test_task_lifecycle_and_idempotency(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("x" * 10, encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "读完了"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            payload = {
                "agent": "deepseek", "prompt": "看一下", "dir": str(root), "mode": "read",
                "user_id": 999999, "admin": True,  # 伪造身份必须被忽略
            }
            first = await http.post(
                "/api/v1/tasks", json=payload, headers={**auth(), "Idempotency-Key": "k1"}
            )
            assert first.status_code == 202
            task_id = first.json()["task_id"]
            again = await http.post(
                "/api/v1/tasks", json=payload, headers={**auth(), "Idempotency-Key": "k1"}
            )
            assert again.json()["task_id"] == task_id
            assert again.json()["idempotent"] is True
            await _drain(app)
            harness.task_store.update_status_if_active(task_id, "succeeded")
            detail = await http.get(f"/api/v1/tasks/{task_id}", headers=auth())
            assert detail.status_code == 200
            record = detail.json()
            assert record["agent"] == "deepseek"
            assert "workspace" not in record and "prompt" not in record
            result = await http.get(f"/api/v1/tasks/{task_id}/result", headers=auth())
            assert result.status_code == 200
            # 归属隔离：另一个 Token 看不到（与不存在同一错误）
            other = await http.get(f"/api/v1/tasks/{task_id}", headers=auth(OTHER_TOKEN))
            assert other.status_code == 404
            missing = await http.get("/api/v1/tasks/deadbeef", headers=auth())
            assert missing.status_code == 404
            assert missing.json()["detail"] == other.json()["detail"]
            # 伪造正文 user_id 不影响归属
            stored = harness.task_store.get_task(task_id)
            assert stored["user_id"] == ALLOWED_USER
    finally:
        await harness.aclose()


async def test_task_without_directory_in_read_mode_is_rejected(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            response = await http.post(
                "/api/v1/tasks",
                json={"agent": "deepseek", "prompt": "读", "mode": "read"},
                headers=auth(),
            )
            assert response.status_code == 400
    finally:
        await harness.aclose()


async def test_task_selection_flow_over_api(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "目标项目").mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "完成"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            created = await http.post(
                "/api/v1/tasks",
                json={"agent": "deepseek", "prompt": "看目录", "find": "目标", "mode": "read"},
                headers=auth(),
            )
            body = created.json()
            assert body["status"] == "awaiting_selection"
            assert body["candidates"]
            assert agent.calls == []
            selected = await http.post(
                "/api/v1/workspaces/select",
                json={"code": body["selection_code"], "index": 1},
                headers=auth(),
            )
            assert selected.status_code == 200
            # 另一个用户不能使用同一确认码（已被消费 → 无效）
            replay = await http.post(
                "/api/v1/workspaces/select",
                json={"code": body["selection_code"], "index": 1},
                headers=auth(OTHER_TOKEN),
            )
            assert replay.status_code == 400
    finally:
        await harness.aclose()


async def test_workspaces_find_endpoint(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "找我的项目").mkdir()
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            response = await http.post(
                "/api/v1/workspaces/find", json={"keyword": "找我"}, headers=auth()
            )
            assert response.status_code == 200
            data = response.json()
            assert data["selection_code"]
            assert any("找我的项目" in path for path in data["candidates"])
    finally:
        await harness.aclose()


# ---------- 团队 ----------


async def test_team_api_flow(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    # V01：read 步骤必须有真实动作回执，"只有 final"会被记为 no_operation 失败
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "团队结论"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            created = await http.post("/api/v1/teams", json={"goal": "目标"}, headers=auth())
            assert created.status_code == 201
            team_id = created.json()["team_id"]
            step = await http.put(
                f"/api/v1/teams/{team_id}/steps/step1",
                json={
                    "agent": "deepseek", "prompt": "读文件", "mode": "read",
                    "dir": str(root),
                },
                headers=auth(),
            )
            assert step.status_code == 200, step.text
            plan = await http.get(f"/api/v1/teams/{team_id}/plan", headers=auth())
            assert plan.status_code == 200
            assert "step1" in plan.json()["plan"]
            run = await http.post(f"/api/v1/teams/{team_id}/run", json={}, headers=auth())
            assert run.status_code == 200
            await harness.wait_team(team_id)
            status = await http.get(f"/api/v1/teams/{team_id}", headers=auth())
            assert "已成功" in status.json()["status"]
            result = await http.get(f"/api/v1/teams/{team_id}/result", headers=auth())
            assert "团队结论" in result.json()["result"]
            # 别的 Token 看不到该团队（与不存在一致）
            other = await http.get(f"/api/v1/teams/{team_id}", headers=auth(OTHER_TOKEN))
            assert "不存在" in other.json()["status"]
            removed = await http.request(
                "DELETE", f"/api/v1/teams/{team_id}/steps/step1",
                json={}, headers=auth(),
            )
            assert removed.status_code in {200, 400}
    finally:
        await harness.aclose()


# ---------- 发现 ----------


async def test_commands_endpoint_matches_registry(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            response = await http.get("/api/v1/commands", headers=auth())
            names = {item["name"] for item in response.json()["commands"]}
            assert names == {"task", "workspace", "agent", "team", "confirm", "access", "command", "help"}
            for legacy in ("read", "write", "all", "codex"):
                assert legacy not in names
    finally:
        await harness.aclose()


# ---------- 复用同一端口（应用装配） ----------


async def test_main_app_keeps_health_and_status_and_mounts_api(tmp_path, monkeypatch) -> None:
    from app import main as main_module
    from helpers import RecordingTransport

    settings = workspace_settings(tmp_path)
    settings.transport = "onebot_http"
    transport = RecordingTransport()

    def fake_build_transport(settings, on_event):
        return transport

    monkeypatch.setattr(main_module, "build_transport", fake_build_transport)
    app = main_module.create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        async with app.router.lifespan_context(app):
            health = await http.get("/healthz")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            status_response = await http.get("/status")
            assert status_response.status_code == 200
            body = status_response.json()
            assert body["status"] == "ok"
            assert body["commands"] == [
                "task", "workspace", "agent", "team", "confirm", "access", "command", "help"
            ]
            assert "backends" in body and "workspaces" in body
            assert body["api_v1_enabled"] is False
            # 没有 Token 配置时 /api/v1 明确返回 503，而不是无鉴权开放
            api = await http.get("/api/v1/agents", headers=auth())
            assert api.status_code == 503


async def test_absolute_paths_not_in_public_lists(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        rows = harness.backends.agent_discovery()
        text = json.dumps(rows, ensure_ascii=False)
        assert str(tmp_path) not in text
        assert "api_key" not in text
    finally:
        await harness.aclose()
