"""内置适配器工厂（登记后可被配置文件按名字引用）。

可运行示例见 `app/backends/mock_adapter.py`（新协议 harness 的最小实现）。
"""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentAdapter
from app.backends.factory import BackendConfig, register_adapter


def _options(config: BackendConfig) -> dict[str, Any]:
    return dict(config.options)


def _build_openai_compatible(config: BackendConfig) -> AgentAdapter:
    from app.agents.openai_compat import OpenAICompatibleAdapter

    raw = _options(config)
    model = config.default_model or str(raw.get("model") or "")
    adapter = OpenAICompatibleAdapter(
        name=config.agent or config.id,
        display_name=config.display_name or config.agent or config.id,
        base_url=str(raw.get("base_url") or ""),
        api_key=config.env(),
        model=model,
        enabled=config.enabled,
        model_options=dict(config.model_options or {model: []}),
    )
    adapter.credential_env = config.credential_env
    adapter.notes = config.notes or adapter.notes
    return adapter


def _build_deepseek_harness(config: BackendConfig) -> AgentAdapter:
    from app.agents.deepseek_harness import DeepSeekHarnessAdapter

    raw = _options(config)
    adapter = DeepSeekHarnessAdapter(
        api_key=config.env(),
        enabled=config.enabled,
        binary=str(raw.get("binary") or raw.get("harness_binary") or ""),
        workdir=str(raw.get("workdir") or "."),
        model=config.default_model or str(raw.get("model") or "deepseek-flash"),
        base_url=str(raw.get("base_url") or "https://api.deepseek.com/anthropic"),
        # 真实模型目录默认来自本机已安装版本；配置可显式覆盖
        model_options={k: list(v) for k, v in (config.model_options or {}).items()} or None,
    )
    adapter.credential_env = config.credential_env
    adapter.notes = config.notes or adapter.notes
    return adapter


def _build_codex_cli(config: BackendConfig) -> AgentAdapter:
    from app.agents.codex import CodexAdapter

    raw = _options(config)
    adapter = CodexAdapter(
        config.enabled,
        binary=str(raw.get("binary") or "codex"),
        workdir=str(raw.get("workdir") or "."),
        model=config.default_model or str(raw.get("model") or ""),
        cache_path=raw.get("cache_path"),
        allowed_roots=tuple(raw.get("allowed_roots") or ()),
    )
    adapter.credential_env = config.credential_env
    adapter.notes = config.notes or adapter.notes
    return adapter


def _build_zcode_stdio(config: BackendConfig) -> AgentAdapter:
    from app.agents.zcode import (
        DEFAULT_BUILTIN,
        DEFAULT_ENTRY,
        DEFAULT_NODE,
        ZCodeAdapter,
    )

    raw = _options(config)
    adapter = ZCodeAdapter(
        api_key=config.env() or str(raw.get("api_key") or ""),
        base_url=str(raw.get("base_url") or ""),
        enabled=config.enabled,
        node=str(raw.get("node") or DEFAULT_NODE),
        entry=str(raw.get("entry") or DEFAULT_ENTRY),
        builtin=str(raw.get("builtin") or DEFAULT_BUILTIN),
        model=config.default_model or str(raw.get("model") or ""),
        catalog=str(raw.get("catalog") or "data/zcode_models.json"),
        workspace_root=str(raw.get("workspace_root") or ""),
        # L04：带工具的模式默认 low（max 在本机实测 60s 超时）；可用 mode_efforts 覆盖
        mode_efforts=raw.get("mode_efforts") if isinstance(raw.get("mode_efforts"), dict) else None,
    )
    adapter.credential_env = config.credential_env
    adapter.notes = config.notes or adapter.notes
    return adapter


def register_builtin_adapters() -> None:
    """幂等登记内置工厂（重复登记直接覆盖同一实现）。

    同时登记 `mock_harness`（app/backends/mock_adapter.py）作为"新协议 harness"
    的可运行示例：只有配置里真的引用它时才会被构造，默认不产生任何行为。
    """
    register_adapter("openai_compatible", _build_openai_compatible)
    register_adapter("deepseek_harness", _build_deepseek_harness)
    register_adapter("codex_cli", _build_codex_cli)
    register_adapter("zcode_stdio", _build_zcode_stdio)
    from app.backends.mock_adapter import register_mock_adapter

    register_mock_adapter()
