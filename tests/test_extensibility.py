"""A03/A04：新增模型/后端/口令的接入成本（不改调度核心）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backends.factory import AdapterFactoryError, known_adapters
from app.backends.mock_adapter import MockHarnessAdapter, register_mock_adapter
from app.backends.registry import BackendConfigError, BackendRegistry
from app.config import Settings
from helpers import ConsoleHarness, FakeAgent, ScriptedAgent, workspace_settings

CORE_FILES = (
    "app/routing/dispatcher.py",
    "app/workflows/runner.py",
    "app/workflows/service.py",
    "app/commands/registry.py",
)


def test_new_same_protocol_model_needs_config_only(tmp_path) -> None:
    """同协议新模型/后端只改 config：Agent 与模型目录自动出现。"""
    settings = Settings(
        _env_file=None,
        qq_allowed_user_ids=[100], qq_allowed_group_ids=[200],
        database_path=str(tmp_path / "t.db"),
        console_backends={
            "corp_llm": {
                "adapter": "openai_compatible",
                "agent": "corp",
                "display_name": "Corp LLM",
                "credential_env": "CORP_LLM_KEY",
                "default_model": "corp-1",
                "model_options": {"corp-1": ["low", "high"], "corp-2": []},
                "base_url": "https://example.invalid/v1",
            }
        },
        console_agents={
            "corp": {"default_backend": "corp_llm", "display_name": "Corp", "default_model": "corp-1"}
        },
    )
    registry = BackendRegistry.from_settings(settings)
    assert registry.all_agent_names() == ["corp"]
    descriptor = registry.get("corp_llm").descriptor
    assert descriptor.models == ("corp-1", "corp-2")
    assert descriptor.model_efforts["corp-1"] == ("low", "high")
    assert descriptor.supports("read") and descriptor.supports("edit")
    assert registry.agent_discovery()[0]["name"] == "corp"


def test_new_protocol_harness_needs_adapter_plus_factory(tmp_path) -> None:
    """新协议 harness：一个 adapter + 一次工厂登记即可，不需要改核心。"""
    register_mock_adapter()
    assert "mock_harness" in known_adapters()
    settings = Settings(
        _env_file=None,
        qq_allowed_user_ids=[100], qq_allowed_group_ids=[200],
        database_path=str(tmp_path / "t.db"),
        console_backends={
            "mock_demo": {
                "adapter": "mock_harness", "agent": "mock", "display_name": "Mock",
                "default_model": "mock-1",
                "responses": [json.dumps({"final": "来自 mock harness"})],
            }
        },
        console_agents={"mock": {"default_backend": "mock_demo"}},
    )
    registry = BackendRegistry.from_settings(settings)
    backend = registry.get("mock_demo")
    assert isinstance(backend.adapter, MockHarnessAdapter)
    # 核心文件里不出现这个后端名字：新增协议 harness 不需要改调度核心
    for path in CORE_FILES:
        assert "mock_harness" not in Path(path).read_text(encoding="utf-8"), path


async def test_mock_harness_works_through_task_and_broker(tmp_path) -> None:
    register_mock_adapter()
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    settings = workspace_settings(
        tmp_path, root=root,
        console_backends={
            "mock_demo": {
                "adapter": "mock_harness", "agent": "mock", "display_name": "Mock",
                "default_model": "mock-1",
                "responses": [
                    json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
                    json.dumps({"final": "读到了 hello"}),
                ],
            }
        },
        console_agents={"mock": {"default_backend": "mock_demo"}},
    )
    harness = ConsoleHarness(settings, {}, from_settings=True)
    try:
        listing = await harness.command("/agent list")
        assert "mock" in listing
        models = await harness.command("/agent models mock")
        assert "mock-1" in models
        reply = await harness.command(f'/task mock --dir "{root}" --mode read -- 读 a.txt')
        assert "读到了 hello" in reply
    finally:
        await harness.aclose()


def test_unknown_adapter_type_is_reported() -> None:
    settings = Settings(
        _env_file=None, qq_allowed_user_ids=[1], qq_allowed_group_ids=[2],
        database_path=":memory:",
        console_backends={"x": {"adapter": "not_registered"}},
        console_agents={"x": {"default_backend": "x"}},
    )
    with pytest.raises(BackendConfigError):
        BackendRegistry.from_settings(settings)
    with pytest.raises(AdapterFactoryError):
        from app.backends.factory import get_factory

        get_factory("not_registered")


def test_agent_pointing_to_missing_backend_fails_fast() -> None:
    settings = Settings(
        _env_file=None, qq_allowed_user_ids=[1], qq_allowed_group_ids=[2],
        database_path=":memory:",
        console_backends={"a": {"adapter": "openai_compatible", "agent": "a"}},
        console_agents={"a": {"default_backend": "missing"}},
    )
    with pytest.raises(BackendConfigError):
        BackendRegistry.from_settings(settings)


def test_registry_deduplicates_and_resolves_backends() -> None:
    registry = BackendRegistry.from_adapters(
        {"deepseek": FakeAgent("deepseek"), "zcode": FakeAgent("zcode")}
    )
    assert registry.get("deepseek").agent == "deepseek"
    assert registry.get("missing") is None
    resolved, error = registry.resolve("deepseek")
    assert resolved.id == "deepseek" and error == ""
    missing, error = registry.resolve("nope")
    assert missing is None and "未知 Agent" in error


async def test_new_preset_requires_no_dispatcher_change(tmp_path) -> None:
    """新增常用口令：只加一个 preset，帮助/discovery/解析自动支持。"""
    from app.commands.presets import PresetSpec

    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(
        settings,
        {"deepseek": ScriptedAgent("deepseek", [json.dumps({"final": "总结完成"})])},
        presets={"summarize": PresetSpec(
            name="summarize", description="总结目录", agent="deepseek", mode="read",
            template="请总结：{task}",
        )},
    )
    try:
        listing = await harness.command("/command list")
        assert "/summarize" in listing
        reply = await harness.command(
            f'/summarize --dir "{tmp_path}" -- 变更点'
        )
        assert "总结完成" in reply
    finally:
        await harness.aclose()
