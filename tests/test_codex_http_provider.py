"""Codex 适配器的受控 HTTP/SSE provider argv 合同（不发任何真实调用）。

背景（evidence/WebSocket握手失败.json、evidence/HTTP同源诊断成功.json）：
本机 `codex-cli 0.162.0-alpha.17.2` 的 Responses WebSocket 在 TLS 握手 EOF，收不到
`turn.completed`；同一 ChatGPT 端点、同一现有 OpenAI 登录、同一模型与权限 profile 的
HTTPS/SSE 诊断真实通过。因此适配器每次调用的 argv 显式声明自定义 provider。

本文件验证的是**合同**：
- 六个配置项必须齐全、按序、逐字一致（不可静默退回 WebSocket 或换端点）；
- 认证保持：`requires_openai_auth=true`，无 `env_key`、无明文令牌、不覆盖内置 openai；
- 传输不降级：base_url 必须 https、`supports_websockets=false`、无 TLS 放宽键；
- 权限不变：`qq_ro`/`qq_files`、`permission_config`、`approval_policy`、web_search 与
  `--sandbox` 缺席等既有安全开关逐项保持；`--ignore-user-config` 保证不读写用户 `~/.codex`。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.agents.codex import (
    BROKER_DISABLE_CANDIDATES,
    HTTP_PROVIDER_BASE_URL,
    HTTP_PROVIDER_ID,
    READ_ONLY_PROFILE,
    CodexAdapter,
    CodexCapabilities,
    _SECRET_LIKE,
    config_pairs,
    http_provider_blockers,
    http_provider_config,
)
from app.models import InvocationOptions


def _adapter(tmp_path: Path, *, native_tools: bool = False) -> CodexAdapter:
    cache = tmp_path / "models.json"
    cache.write_text(json.dumps({"models": [{
        "slug": "test-model", "supported_reasoning_levels": [{"effort": "high"}]}]}),
        encoding="utf-8")
    agent = CodexAdapter(True, binary="true", workdir=str(tmp_path),
                         cache_path=str(cache), allowed_roots=[str(tmp_path)])
    agent._login_present = True
    agent._capabilities = CodexCapabilities(
        binary="true", version="codex-cli 0.162.0-alpha.17.2",
        features=frozenset(BROKER_DISABLE_CANDIDATES), probed=True,
    )
    if native_tools:
        agent.runtime_options = {"native_tools": True}
    return agent


def _options(tmp_path: Path, mode: str = "text") -> InvocationOptions:
    directory = str(tmp_path) if mode != "text" else None
    return InvocationOptions("test-model", mode=mode, directory=directory,
                             workspace_tools=mode != "text")


def _build(agent: CodexAdapter, tmp_path: Path, *, native: bool, mode: str):
    return agent.build_argv(options=_options(tmp_path, mode), native=native,
                            directory=str(tmp_path))


def _replace_config(argv, key: str, value: str) -> list[str]:
    """把 argv 里 `-c key=旧值` 换成新值（用于构造降级/篡改用例）。"""
    items = [str(item) for item in argv]
    for index, item in enumerate(items):
        if item == "-c" and index + 1 < len(items) and items[index + 1].startswith(key + "="):
            items[index + 1] = f"{key}={value}"
            return items
    raise AssertionError(f"argv 缺少配置：{key}")


# ---------- 1) 六个配置项齐全、按序、逐字一致 ----------


@pytest.mark.parametrize("native,mode", [(False, "text"), (False, "edit"), (True, "exec")])
def test_build_argv_carries_exact_http_provider_contract(tmp_path, native, mode) -> None:
    agent = _adapter(tmp_path, native_tools=native)
    argv, _ = _build(agent, tmp_path, native=native, mode=mode)
    expected = [f"{key}={value}" for key, value in http_provider_config()]
    start = argv.index(expected[0])
    assert argv[start:start + 2 * len(expected):2] == expected
    assert argv[start + 1:start + 2 * len(expected):2] == ["-c"] * len(expected)
    assert http_provider_blockers(argv) == []


def test_provider_argv_function_matches_contract() -> None:
    from app.agents.codex import http_provider_argv

    pairs = config_pairs(http_provider_argv())
    assert pairs == list(http_provider_config())
    assert len(pairs) == 6


# ---------- 2) 认证保持、传输不降级 ----------


def test_provider_preserves_existing_login_without_secrets(tmp_path) -> None:
    agent = _adapter(tmp_path)
    argv, _ = _build(agent, tmp_path, native=False, mode="text")
    values = dict(config_pairs(argv))
    prefix = f"model_providers.{HTTP_PROVIDER_ID}"
    assert values["model_provider"] == json.dumps(HTTP_PROVIDER_ID)
    assert values[f"{prefix}.requires_openai_auth"] == "true"
    assert values[f"{prefix}.supports_websockets"] == "false"
    assert values[f"{prefix}.wire_api"] == json.dumps("responses")
    assert values[f"{prefix}.base_url"] == json.dumps(HTTP_PROVIDER_BASE_URL)
    assert HTTP_PROVIDER_BASE_URL == "https://chatgpt.com/backend-api/codex"
    joined = " ".join(str(item) for item in argv)
    assert "env_key" not in joined
    assert not _SECRET_LIKE.search(joined)
    assert "model_providers.openai" not in joined
    assert "--ignore-user-config" in argv


@pytest.mark.parametrize("case,needle", [
    ("auth_false", "requires_openai_auth"),
    ("websockets_true", "supports_websockets"),
    ("wire_api_chat", "wire_api"),
    ("base_url_http", "https"),
    ("env_key", "env_key"),
    ("builtin_openai", "内置 openai"),
    ("tls_loosen", "传输安全"),
    ("no_ignore_user_config", "--ignore-user-config"),
    ("missing_provider", "缺少受控 HTTP provider 配置"),
])
def test_provider_contract_rejects_downgrades(tmp_path, case, needle) -> None:
    agent = _adapter(tmp_path)
    argv, _ = _build(agent, tmp_path, native=False, mode="text")
    prefix = f"model_providers.{HTTP_PROVIDER_ID}"
    if case == "auth_false":
        tampered = _replace_config(argv, f"{prefix}.requires_openai_auth", "false")
    elif case == "websockets_true":
        tampered = _replace_config(argv, f"{prefix}.supports_websockets", "true")
    elif case == "wire_api_chat":
        tampered = _replace_config(argv, f"{prefix}.wire_api", json.dumps("chat"))
    elif case == "base_url_http":
        tampered = _replace_config(argv, f"{prefix}.base_url",
                                   json.dumps("http://chatgpt.com/backend-api/codex"))
    elif case == "env_key":
        tampered = [*argv, "-c", f'{prefix}.env_key="OPENAI_API_KEY"']
    elif case == "builtin_openai":
        tampered = _replace_config(
            argv, "model_provider", json.dumps("openai"),
        ) + ["-c", 'model_providers.openai.base_url="https://example.invalid"']
    elif case == "tls_loosen":
        tampered = [*argv, "-c", f"{prefix}.tls_verify=false"]
    elif case == "no_ignore_user_config":
        tampered = [item for item in argv if str(item) != "--ignore-user-config"]
    else:
        expected = [f"{key}={value}" for key, value in http_provider_config()]
        tampered = [item for item in argv if str(item) not in set(expected)]
    problems = http_provider_blockers(tampered)
    assert problems, case
    assert any(needle in problem for problem in problems), (case, problems)


def test_provider_blockers_are_advisory_when_block_absent(tmp_path) -> None:
    """安全闸门口径：适配器之外构造的 argv 不含本块时不因此拒绝启动。"""
    agent = _adapter(tmp_path)
    argv, _ = _build(agent, tmp_path, native=False, mode="text")
    expected = {f"{key}={value}" for key, value in http_provider_config()}
    stripped = [item for item in argv if str(item) not in expected]
    assert http_provider_blockers(stripped) != []
    assert http_provider_blockers(stripped, require_present=False) == []


# ---------- 3) 权限与既有安全开关不变 ----------


def test_provider_does_not_change_permission_argv(tmp_path) -> None:
    agent = _adapter(tmp_path, native_tools=True)
    broker, _ = _build(agent, tmp_path, native=False, mode="edit")
    joined = " ".join(str(item) for item in broker)
    assert f'default_permissions="{READ_ONLY_PROFILE}"' in broker
    assert f"permissions.{READ_ONLY_PROFILE}=" in joined
    assert "permissions.qq_files=" not in joined
    assert 'approval_policy="never"' in broker
    assert 'shell_environment_policy.inherit="none"' in broker
    assert 'web_search="disabled"' in broker
    assert "--sandbox" not in broker
    assert not agent.sandbox_policy_blockers(broker)
    native_exec, _ = _build(agent, tmp_path, native=True, mode="exec")
    assert 'default_permissions="qq_files"' in native_exec
    assert "permissions.qq_files=" in " ".join(str(item) for item in native_exec)
    assert not agent.sandbox_policy_blockers(native_exec)
    assert http_provider_blockers(broker) == []
    assert http_provider_blockers(native_exec) == []


def test_sandbox_gate_accepts_production_argv_and_rejects_tampering(tmp_path) -> None:
    agent = _adapter(tmp_path)
    argv, _ = _build(agent, tmp_path, native=False, mode="text")
    assert agent._sandbox_ready(native=False, mode="text", argv=argv) == (True, "")
    tampered = _replace_config(
        argv, f"model_providers.{HTTP_PROVIDER_ID}.requires_openai_auth", "false")
    ready, reason = agent._sandbox_ready(native=False, mode="text", argv=tampered)
    assert ready is False
    assert "requires_openai_auth" in reason


# ---------- 4) 只作用于本适配器，不读写用户 ~/.codex ----------


def test_build_argv_never_touches_user_codex_config(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    agent = _adapter(tmp_path)
    argv, _ = _build(agent, tmp_path, native=False, mode="text")
    assert "--ignore-user-config" in argv
    assert all("config.toml" not in str(item) for item in argv)
    assert not (home / ".codex").exists()


def test_provider_block_is_scoped_to_codex_adapter() -> None:
    import app.agents.codex as codex_module
    from app.agents import deepseek_harness, zcode

    assert HTTP_PROVIDER_ID in inspect.getsource(codex_module)
    for module in (deepseek_harness, zcode):
        assert HTTP_PROVIDER_ID not in inspect.getsource(module), module.__name__
