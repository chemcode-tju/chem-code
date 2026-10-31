"""Per-workspace-user model preferences; secrets never leave the backend."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["chatgpt", "deepseek", "openai-compatible"] = "chatgpt"
    model: str = Field(default="", max_length=160)
    base_url: str = Field(default="https://api.deepseek.com", max_length=2048)
    api_key: SecretStr | None = None
    thinking: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("服务地址必须是无账号、查询参数和片段的 HTTP(S) URL。")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程服务必须使用 HTTPS；HTTP 仅允许本机模型。")
        return value.rstrip("/")

    @field_validator("api_key", mode="before")
    @classmethod
    def clean_key(cls, value: object) -> object:
        if isinstance(value, str):
            if len(value) > 8192 or "\n" in value or "\r" in value:
                raise ValueError("API Key 格式无效。")
            return value.strip() or None
        return value

    def public(self) -> dict:
        return self.model_dump(exclude={"api_key"}) | {"has_api_key": bool(self.api_key)}


class ModelSettingsStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.database_path.parent / "model-credentials"

    def directory(self, user_id: str | None) -> Path:
        identifier = hashlib.sha256((user_id or "local").encode()).hexdigest()
        return self.root / identifier

    def load(self, user_id: str | None) -> ModelSettings:
        path = self.directory(user_id) / "settings.json"
        if path.exists():
            try:
                return ModelSettings.model_validate_json(path.read_text())
            except (ValueError, OSError) as exc:
                raise AgentError(
                    "model_settings_invalid", "模型配置文件损坏，请重新保存设置。", 503
                ) from exc
        if self.settings.model_configured:
            return ModelSettings.model_construct(
                provider="deepseek",
                model=self.settings.deepseek_model,
                base_url=self.settings.deepseek_base_url,
                api_key=self.settings.deepseek_api_key,
                thinking=self.settings.deepseek_thinking,
            )
        return ModelSettings()

    def save(self, user_id: str | None, incoming: ModelSettings) -> ModelSettings:
        """A blank key retains a saved key only for the exact same destination."""
        try:
            previous = self.load(user_id)
        except AgentError:
            previous = ModelSettings()
        if incoming.provider != "chatgpt" and not incoming.model:
            raise AgentError("model_required", "请输入服务商提供的模型 ID。", 422)
        if incoming.api_key is None and (incoming.provider, incoming.base_url) == (
            previous.provider,
            previous.base_url,
        ):
            incoming = incoming.model_copy(update={"api_key": previous.api_key})
        if incoming.provider == "chatgpt":
            incoming = incoming.model_copy(update={"api_key": None})
        elif not incoming.api_key and urlsplit(incoming.base_url).hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise AgentError(
                "key_required", "此服务地址需要填写 API Key。更换服务时请重新填写。", 422
            )
        directory = self.directory(user_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        data = incoming.model_dump(exclude={"api_key"})
        data["api_key"] = incoming.api_key.get_secret_value() if incoming.api_key else None
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".settings-")
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(data, output, ensure_ascii=False)
            Path(temporary).replace(directory / "settings.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
        return incoming

    def clear_key(self, user_id: str | None) -> None:
        """Persist a keyless default instead of silently restoring an environment key."""
        self.save(user_id, ModelSettings())
