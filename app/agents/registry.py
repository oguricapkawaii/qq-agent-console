"""Agent 视图（兼容层）：把 BackendRegistry 的「一个 Agent 一个默认后端」暴露为适配器映射。

统一控制台的分层是 Provider / Backend / Agent。本模块只是**读取视图**：
- 新代码请直接使用 `app.backends.registry.BackendRegistry`（能力、后端、Agent 完整信息）；
- 保留 `AgentRegistry` 是为了让既有调用点与测试可以平滑迁移，不再承载任何业务分支。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.agents.base import AgentAdapter
from app.backends.registry import BackendRegistry


class AgentRegistry:
    def __init__(
        self, agents: Mapping[str, AgentAdapter] | None = None, *,
        backends: BackendRegistry | None = None, exec_policy_enabled: bool = False,
    ) -> None:
        if backends is not None:
            self._backends = backends
        else:
            self._backends = BackendRegistry.from_adapters(
                agents or {}, exec_policy_enabled=exec_policy_enabled
            )

    @classmethod
    def from_settings(cls, settings) -> "AgentRegistry":
        return cls(backends=BackendRegistry.from_settings(settings))

    @classmethod
    def from_backends(cls, backends: BackendRegistry) -> "AgentRegistry":
        return cls(backends=backends)

    @property
    def backends(self) -> BackendRegistry:
        return self._backends

    # ---------- 兼容查询 ----------

    def get(self, name: str) -> AgentAdapter | None:
        backend, _ = self._backends.resolve(name)
        return backend.adapter if backend is not None else None

    def all_agents(self) -> list[AgentAdapter]:
        adapters: list[AgentAdapter] = []
        for agent in self._backends.all_agents():
            backend = self._backends.get(agent.default_backend)
            if backend is not None and backend.adapter not in adapters:
                adapters.append(backend.adapter)
        return adapters

    def all_agent_names(self) -> list[str]:
        return self._backends.all_agent_names()

    def configured_agents(self) -> list[AgentAdapter]:
        names = {agent.name for agent in self._backends.configured_agents()}
        return [adapter for adapter in self.all_agents() if adapter.name in names]

    async def aclose(self) -> None:
        await self._backends.aclose()
