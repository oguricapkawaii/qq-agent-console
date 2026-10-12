"""应用入口冒烟测试：create_app 装配、健康检查与状态接口。"""

from __future__ import annotations

import httpx

from app.main import create_app
from helpers import RecordingTransport, make_settings


def test_create_app_builds(tmp_path):
    app = create_app(make_settings(tmp_path))
    assert app.title == "QQ 多模型统一控制台"


async def test_healthz_endpoint(tmp_path):
    app = create_app(make_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_status_endpoint_before_lifespan(tmp_path):
    """未经过 lifespan（未启动）时 /status 返回 starting。"""
    app = create_app(make_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/status")
        assert response.status_code == 200
        assert response.json()["status"] == "starting"


async def test_full_lifespan_with_fake_transport(tmp_path, monkeypatch):
    """用假传输替换真实 WS 连接，验证 lifespan 完整启动/停止与 /status 内容。"""
    created: list[RecordingTransport] = []

    def fake_build_transport(settings, on_event):
        transport = RecordingTransport()
        created.append(transport)
        return transport

    import app.main as main_module

    monkeypatch.setattr(main_module, "build_transport", fake_build_transport)

    app = create_app(make_settings(tmp_path))
    # httpx.ASGITransport 不执行 lifespan，需要手动进入 lifespan 上下文
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/status")
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "ok"
            # 默认配置（无 API Key）：agents 列表存在但没有已配置项
            assert isinstance(payload["agents"], list)
            assert all(not agent["configured"] for agent in payload["agents"])
            # 待确认数量（原 pending_approvals 的替代）与限额
            assert payload["pending_confirmations"] == 0
            assert payload["limits"]["max_context_turns"] == 8
            # 命令清单来自注册表，不再是硬编码列表
            for name in ("task", "workspace", "agent", "team", "confirm", "access", "command", "help"):
                assert name in payload["commands"]
            assert payload["presets"] == []
            assert isinstance(payload["workspaces"]["roots"], list)
            assert payload["api_v1_enabled"] is False
    assert created, "应创建传输"
