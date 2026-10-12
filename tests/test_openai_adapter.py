"""OpenAI 兼容适配器测试（httpx.MockTransport，离线）。"""

from __future__ import annotations

import json

import httpx

from app.agents.openai_compat import OpenAICompatibleAdapter
from app.models import ChatMessage

API_KEY = "sk-test-key-1234567890"


def make_adapter(handler, *, api_key: str = API_KEY, enabled: bool = True) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        name="gpt", display_name="GPT",
        base_url="https://api.test/v1", api_key=api_key, model="test-model",
        enabled=enabled, http_transport=httpx.MockTransport(handler),
    )


async def test_success_returns_content():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "你好，世界"}}]})

    adapter = make_adapter(handler)
    reply = await adapter.chat([ChatMessage(role="user", content="hi")], timeout=5)
    assert reply.error is None
    assert reply.content == "你好，世界"


async def test_request_url_payload_and_auth_header():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = make_adapter(handler)
    await adapter.chat(
        [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u")],
        timeout=5,
    )
    assert captured["url"] == "https://api.test/v1/chat/completions"
    assert captured["auth"] == f"Bearer {API_KEY}"
    assert captured["body"]["model"] == "test-model"
    assert [m["role"] for m in captured["body"]["messages"]] == ["system", "user"]


async def test_http_error_maps_to_short_api_error():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid key"})

    reply = await make_adapter(handler).chat([ChatMessage(role="user", content="x")], timeout=5)
    assert reply.error == "api_error_401"
    assert API_KEY not in reply.error


async def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("read timed out")

    reply = await make_adapter(handler).chat([ChatMessage(role="user", content="x")], timeout=5)
    assert reply.error == "timeout"


async def test_network_error_maps_to_network():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    reply = await make_adapter(handler).chat([ChatMessage(role="user", content="x")], timeout=5)
    assert reply.error == "network"


async def test_malformed_response_maps_to_parse_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    reply = await make_adapter(handler).chat([ChatMessage(role="user", content="x")], timeout=5)
    assert reply.error == "parse"


async def test_missing_api_key_is_not_configured():
    """缺少密钥：configured=False，不发起任何网络请求。"""
    def handler(request):  # pragma: no cover - 不应被调用
        raise AssertionError("不应发起请求")

    adapter = make_adapter(handler, api_key="")
    assert not adapter.configured
    reply = await adapter.chat([ChatMessage(role="user", content="x")], timeout=5)
    assert reply.error == "not_configured"


async def test_disabled_agent_is_not_configured():
    def handler(request):  # pragma: no cover
        raise AssertionError("不应发起请求")

    adapter = make_adapter(handler, enabled=False)
    assert not adapter.configured


async def test_registry_missing_one_key_others_still_run(tmp_path, monkeypatch):
    """缺少 GLM 的密钥时，DeepSeek/GPT 仍然可运行。

    统一架构里凭据只来自环境变量（各后端声明 credential_env），
    不再从 Settings 的 per-agent api_key 字段读取。
    """
    from app.agents.registry import AgentRegistry
    from app.config import Settings

    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-key-12345678")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-key-12345678")

    settings = Settings(
        _env_file=None,
        qq_allowed_user_ids=[1], qq_allowed_group_ids=[1],
    )
    registry = AgentRegistry.from_settings(settings)
    assert not registry.get("glm").configured
    assert registry.get("deepseek").configured
    assert registry.get("gpt").configured
    assert [agent.name for agent in registry.configured_agents()] == ["deepseek", "gpt"]


async def test_registry_respects_enabled_flags(tmp_path, monkeypatch):
    from app.agents.registry import AgentRegistry
    from app.config import Settings

    monkeypatch.setenv("GLM_API_KEY", "sk-1-1234567890")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-2-1234567890")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-3-1234567890")

    settings = Settings(
        _env_file=None,
        glm_enabled=False, deepseek_enabled=True, openai_enabled=False,
    )
    registry = AgentRegistry.from_settings(settings)
    assert not registry.get("glm").configured  # 有 key 但被禁用
    assert registry.get("deepseek").configured
    assert not registry.get("gpt").configured
    assert registry.get("codex") is not None
    assert not registry.get("codex").configured
