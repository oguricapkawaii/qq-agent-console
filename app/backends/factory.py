"""后端适配器工厂注册表。

新增一个「已有类型」的后端只需改配置；新增一个「新协议 harness」只需：
1. 写一个 AgentAdapter 子类；
2. 用 ``register_adapter("my_adapter", factory)`` 登记工厂。
Dispatcher、TaskService、team scheduler 都不需要改动。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app.agents.base import AgentAdapter


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """一个后端的声明式配置（来自 YAML/环境变量）。"""

    id: str
    adapter: str
    agent: str = ""
    display_name: str = ""
    enabled: bool = True
    credential_env: str = ""
    default_model: str | None = None
    model_options: Mapping[str, list[str]] | None = None
    options: Mapping[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    # 由 Settings（含 .env）解析出的凭据：环境变量缺失时作为回退。
    # 优先级与项目既有约定一致：进程环境变量 > .env（Settings） > YAML。
    credentials: Mapping[str, str] = field(default_factory=dict)

    def option(self, key: str, default=None):
        return self.options.get(key, default)

    def env(self, name: str | None = None) -> str:
        key = name or self.credential_env
        if not key:
            return ""
        value = os.environ.get(key, "")
        if value:
            return value
        return str(self.credentials.get(key) or "")


AdapterFactory = Callable[[BackendConfig], AgentAdapter]

_FACTORIES: dict[str, AdapterFactory] = {}


class AdapterFactoryError(ValueError):
    """未知或非法的适配器类型。"""


def register_adapter(adapter_type: str, factory: AdapterFactory) -> None:
    """登记一个适配器工厂（新协议 harness 的唯一接入点）。"""
    if not adapter_type:
        raise AdapterFactoryError("适配器类型不能为空")
    _FACTORIES[adapter_type] = factory


def get_factory(adapter_type: str) -> AdapterFactory:
    factory = _FACTORIES.get(adapter_type)
    if factory is None:
        known = ", ".join(sorted(_FACTORIES)) or "（无）"
        raise AdapterFactoryError(
            f"未知适配器类型：{adapter_type}；已登记：{known}。"
            "新增协议 harness 请实现 AgentAdapter 并 register_adapter() 登记工厂。"
        )
    return factory


def known_adapters() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_backend(config: BackendConfig) -> AgentAdapter:
    return get_factory(config.adapter)(config)
