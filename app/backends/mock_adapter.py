"""可运行示例：一个新协议 harness 的最小适配器 + 工厂登记。

用途（统一架构第 8 节 / 验收 A04）：
- 证明"新增一个协议 harness"只需要「一个 adapter + 一次工厂登记」，
  不需要修改 Dispatcher、TaskService 或 team scheduler；
- 配置示例（config.example.yaml 的 `backends.mock_demo`）：
  ``adapter: mock_harness`` + ``responses: [...]``；
- 它不联网、不起子进程，按脚本依次回放答复，用于离线验证与回归测试。

新协议 harness 的真实写法与这里一致：把"启动进程/发送请求/解析事件/处理取消"
全部封在 `chat()` 内，并把能力如实写进类属性。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

from app.agents.base import AgentAdapter
from app.backends.factory import BackendConfig, register_adapter
from app.commands.options import MODE_EDIT, MODE_READ
from app.models import AgentReply, ChatMessage, InvocationOptions


class MockHarnessAdapter(AgentAdapter):
    """按脚本回放的假 harness：用于离线验证统一接入。"""

    adapter_type = "mock_harness"
    isolation = "broker"
    native_modes = frozenset()
    broker_modes = frozenset({MODE_READ, MODE_EDIT})
    notes = ("离线示例适配器：不联网、不起子进程，按脚本回放答复。",)

    def __init__(
        self, *, name: str, display_name: str, model: str = "mock-1",
        responses: Sequence[str] | None = None, enabled: bool = True,
        model_options: dict | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.model_name = model
        self.enabled = enabled
        self._responses = list(responses or ['{"final": "mock reply"}'])
        self._cursor = 0
        self._model_options = {
            key: tuple(value) for key, value in (model_options or {model: ()}).items()
        }
        self.calls: list[list[ChatMessage]] = []

    @property
    def configured(self) -> bool:
        return self.enabled

    @property
    def model_options(self) -> dict[str, tuple[str, ...]]:
        return dict(self._model_options)

    def push(self, text: str) -> None:
        self._responses.append(text)

    async def chat(self, messages: Sequence[ChatMessage], *, timeout: float) -> AgentReply:
        self.calls.append(list(messages))
        await asyncio.sleep(0)
        if timeout <= 0:
            return AgentReply(error="timeout")
        if self._cursor >= len(self._responses):
            return AgentReply(error="harness_failed")
        text = self._responses[self._cursor]
        self._cursor += 1
        return AgentReply(content=text)

    async def chat_with_options(
        self, messages: Sequence[ChatMessage], *, timeout: float, options: InvocationOptions
    ) -> AgentReply:
        self.validate_options(options)
        return await self.chat(messages, timeout=timeout)


def _build_mock_harness(config: BackendConfig) -> AgentAdapter:
    raw = dict(config.options)
    responses = raw.get("responses")
    if isinstance(responses, str):
        try:
            responses = json.loads(responses)
        except ValueError:
            responses = [responses]
    return MockHarnessAdapter(
        name=config.agent or config.id,
        display_name=config.display_name or config.agent or config.id,
        model=config.default_model or str(raw.get("model") or "mock-1"),
        responses=tuple(str(item) for item in responses) if responses else None,
        enabled=config.enabled,
        model_options={k: list(v) for k, v in (config.model_options or {}).items()} or None,
    )


def register_mock_adapter() -> None:
    """登记示例工厂；该函数在应用装配与示例文档中都被显式调用。"""
    register_adapter("mock_harness", _build_mock_harness)
