"""OpenAI 兼容适配器：GPT / DeepSeek / GLM 等共用，各自配置 base_url / api_key / model。"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from app.agents.base import AgentAdapter
from app.models import AgentReply, ChatMessage, InvocationOptions

logger = logging.getLogger("qqbot.agent.openai_compat")


class OpenAICompatibleAdapter(AgentAdapter):
    adapter_type = "openai_compatible"
    # 纯 HTTP 文本协议：原生没有任何文件/命令工具，read/edit 由公共 broker 提供
    isolation = "broker"
    native_modes = frozenset()
    broker_modes = frozenset({"read", "edit"})
    notes = ("文本 API 后端：目录读写通过公共文件 broker 完成。",)

    def __init__(
        self,
        *,
        name: str,
        display_name: str,
        base_url: str,
        api_key: str,
        model: str,
        enabled: bool = True,
        model_options: dict[str, list[str]] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.model_name = model
        self.enabled = enabled
        self._model_options = {key: tuple(values) for key, values in (model_options or {model: []}).items()}
        self._api_key = (api_key or "").strip()
        self._client = httpx.AsyncClient(
            base_url=(base_url or "").rstrip("/"),
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
            transport=http_transport,
        )

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self._api_key)

    @property
    def model_options(self) -> dict[str, tuple[str, ...]]:
        return dict(self._model_options)

    async def chat(self, messages: Sequence[ChatMessage], *, timeout: float) -> AgentReply:
        return await self.chat_with_options(messages, timeout=timeout, options=InvocationOptions(self.model_name))

    async def chat_with_options(self, messages: Sequence[ChatMessage], *, timeout: float,
                                options: InvocationOptions) -> AgentReply:
        self.validate_options(options)
        if not self.configured:
            return AgentReply(error="not_configured")
        payload = {
            "model": options.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if options.effort is not None:
            payload["reasoning_effort"] = options.effort
        try:
            response = await self._client.post(
                "/chat/completions", json=payload, timeout=timeout
            )
        except httpx.TimeoutException:
            return AgentReply(error="timeout")
        except httpx.HTTPError:
            # 网络层错误消息可能包含内部细节，只返回类别
            return AgentReply(error="network")
        if response.status_code >= 400:
            # 只透出状态码，不透出响应体（可能含敏感信息）
            return AgentReply(error=f"api_error_{response.status_code}")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return AgentReply(error="parse")
        return AgentReply(content=str(content) if content is not None else "")

    async def aclose(self) -> None:
        await self._client.aclose()
