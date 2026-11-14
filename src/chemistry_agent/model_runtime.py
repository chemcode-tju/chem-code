"""Select a model per request without mutating a shared agent or its settings."""

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from chemistry_agent.chatgpt import ChatGPTClient
from chemistry_agent.chatgpt_responses import model_window
from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError
from chemistry_agent.model_settings import ModelSettings, ModelSettingsStore
from chemistry_agent.provider import Completion, DeepSeekClient


@dataclass
class ModelScope:
    user_id: str | None
    configuration: ModelSettings | None = None
    default_configuration: ModelSettings | None = None


current_model_scope: ContextVar[ModelScope | None] = ContextVar("model_scope", default=None)
current_effort: ContextVar[str] = ContextVar("reasoning_effort", default="")


class ProviderRouter:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.store = ModelSettingsStore(settings)
        self.chatgpt = ChatGPTClient(self.store)
        self.window_catalogs: dict[tuple, tuple[float, list[dict]]] = {}

    def configuration(self) -> tuple[str | None, ModelSettings]:
        scope = current_model_scope.get()
        if scope is None:
            raise AgentError("model_scope_missing", "缺少用户模型配置上下文。", 500)
        if scope.configuration is None:
            scope.configuration = self.store.load(scope.user_id)
        if scope.default_configuration is None:
            scope.default_configuration = scope.configuration.model_copy(deep=True)
        return scope.user_id, scope.configuration

    def select_model(self, model: str | None) -> ModelSettings:
        """Override only this request; None restores its frozen default configuration."""
        self.configuration()
        scope = current_model_scope.get()
        assert scope is not None and scope.default_configuration is not None
        default = scope.default_configuration
        scope.configuration = default.model_copy(
            update={"model": model if model is not None else default.model}, deep=True
        )
        return scope.configuration

    async def context_window(self) -> int:
        """Resolve the active subscription model; API providers retain their configured limit."""
        user_id, configuration = self.configuration()
        fallback = self.settings.context_window_tokens
        if configuration.provider != "chatgpt":
            return fallback
        account = self.chatgpt.plan.load(user_id)
        if account.get("selected"):
            key = (user_id, account.get("active"))
            expires, entries = self.window_catalogs.get(key, (0, []))
            if time.monotonic() >= expires:
                try:
                    entries = await self.chatgpt.responses.list_models(user_id)
                except AgentError:
                    if not entries:
                        return fallback
                else:
                    self.window_catalogs[key] = (time.monotonic() + 300, entries)
            selected = next((m for m in entries if m["id"] == configuration.model), None)
            if not configuration.model and entries:
                selected = entries[0]
                self.select_model(selected["id"])  # Use the same model for sizing and inference.
            window = selected.get("window") if selected else None
        else:
            path = self.store.directory(user_id) / "codex" / "models_cache.json"
            try:
                entries = json.loads(path.read_text())["models"]
                entry = next((m for m in entries if m.get("slug") == configuration.model), {})
                window = model_window(entry)
            except (OSError, ValueError, AttributeError, TypeError, KeyError):
                window = None
        return window or fallback

    async def list_models(self) -> dict:
        """Fetch a bounded catalog from the saved destination using only its saved key."""
        user_id, configuration = self.configuration()
        if configuration.provider == "chatgpt":
            entries = await self.chatgpt.list_models(user_id)
            source = "chatgpt"
        else:
            headers = {"Accept": "application/json"}
            if configuration.api_key:
                headers["Authorization"] = f"Bearer {configuration.api_key.get_secret_value()}"
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(15, connect=5),
                    transport=self.transport,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "GET", configuration.base_url.rstrip("/") + "/models", headers=headers
                    ) as response:
                        if response.status_code in {401, 403}:
                            raise AgentError(
                                "model_catalog_auth_failed",
                                "模型列表认证失败，请检查已保存的登录或密钥。",
                                502,
                            )
                        if response.status_code != 200:
                            raise AgentError(
                                "model_catalog_unavailable",
                                "服务暂未提供可用的模型列表，可手动填写模型 ID。",
                                502,
                            )
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 1_000_000:
                                raise ValueError("catalog too large")
                            chunks.append(chunk)
                        import json

                        payload = json.loads(b"".join(chunks))
                        entries = payload.get("data") if isinstance(payload, dict) else None
            except (httpx.HTTPError, ValueError) as exc:
                raise AgentError(
                    "model_catalog_unavailable", "无法读取模型列表，请检查服务地址或稍后重试。", 502
                ) from exc
            source = "api"
        if not isinstance(entries, list) or len(entries) > 5000:
            raise AgentError("model_catalog_invalid", "模型列表格式无效。", 502)
        models, seen = [], set()
        key = configuration.api_key.get_secret_value() if configuration.api_key else None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            if (
                not isinstance(identifier, str)
                or not 1 <= len(identifier) <= 160
                or any(ord(char) <= 32 or ord(char) == 127 for char in identifier)
                or identifier in seen
                or (key and key in identifier)
            ):
                continue
            name = entry.get("name")
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 240
                or any(ord(char) < 32 for char in name)
                or (key and key in name)
            ):
                name = identifier
            seen.add(identifier)
            efforts = entry.get("efforts", []) if configuration.provider == "chatgpt" else []
            if configuration.provider == "deepseek":
                efforts = ["disabled", "enabled"]
            supported = {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
                "ultra",
                "disabled",
                "enabled",
            }
            efforts = (
                [value for value in efforts if isinstance(value, str) and value in supported]
                if isinstance(efforts, list)
                else []
            )
            models.append(
                {"id": identifier, "name": name, **({"efforts": efforts} if efforts else {})}
            )
            if len(models) == 1000:
                break
        return {"provider": configuration.provider, "models": models, "source": source}

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
        effort = current_effort.get()
        if effort in {"enabled", "disabled"}:
            thinking = effort == "enabled"
        if configuration.provider == "chatgpt":
            return await self.chatgpt.complete(
                user_id,
                configuration,
                messages,
                tools,
                allow_tools=allow_tools,
                max_tokens=max_tokens,
                thinking=thinking,
                effort=effort,
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
