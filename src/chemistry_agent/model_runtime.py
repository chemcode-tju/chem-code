"""Select a model per request without mutating a shared agent or its settings."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from chemistry_agent.chatgpt import ChatGPTClient
from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError
from chemistry_agent.model_settings import ModelSettings, ModelSettingsStore
from chemistry_agent.provider import Completion, DeepSeekClient


@dataclass
class ModelScope:
    user_id: str | None
    configuration: ModelSettings | None = None


current_model_scope: ContextVar[ModelScope | None] = ContextVar("model_scope", default=None)


class ProviderRouter:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.store = ModelSettingsStore(settings)
        self.chatgpt = ChatGPTClient(self.store)

    def configuration(self) -> tuple[str | None, ModelSettings]:
        scope = current_model_scope.get()
        if scope is None:
            raise AgentError("model_scope_missing", "缺少用户模型配置上下文。", 500)
        if scope.configuration is None:
            scope.configuration = self.store.load(scope.user_id)
        return scope.user_id, scope.configuration

    async def ready(self) -> bool:
        user_id, configuration = self.configuration()
        if configuration.provider == "chatgpt":
            return await self.chatgpt.authenticated(user_id)
        return bool(
            configuration.model
            and (configuration.api_key or configuration.provider == "openai-compatible")
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        allow_tools: bool,
        max_tokens: int | None = None,
        thinking: bool | None = None,
    ) -> Completion:
        user_id, configuration = self.configuration()
        if configuration.provider == "chatgpt":
            return await self.chatgpt.complete(
                user_id,
                configuration,
                messages,
                tools,
                allow_tools=allow_tools,
                max_tokens=max_tokens,
                thinking=thinking,
            )
        settings = self.settings.model_copy(
            update={
                "deepseek_api_key": configuration.api_key,
                "deepseek_base_url": configuration.base_url,
                "deepseek_model": configuration.model,
                "deepseek_thinking": configuration.thinking,
            }
        )
        provider = DeepSeekClient(
            settings,
            transport=self.transport,
            compatible=configuration.provider == "openai-compatible",
        )
        try:
            return await provider.complete(
                messages,
                tools,
                allow_tools=allow_tools,
                max_tokens=max_tokens,
                thinking=thinking,
            )
        except AgentError as exc:
            if configuration.provider == "openai-compatible":
                exc.message = exc.message.replace("DeepSeek", "模型服务")
            raise
        finally:
            await provider.close()

    async def close(self) -> None:
        await self.chatgpt.close()
