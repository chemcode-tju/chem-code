"""Private connection configuration; API keys never appear in model tool results."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from chemistry_agent.automation.ssh_profiles import write_private


class KnowledgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:1933"
    api_key: SecretStr | None = None
    root_uri: str = "viking://~/resources/chem-code"

    @field_validator("endpoint")
    @classmethod
    def endpoint_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("HTTP(S) endpoint required")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Credentials belong in the API key field")
        return value.rstrip("/")

    @field_validator("root_uri")
    @classmethod
    def namespace_uri(cls, value: str) -> str:
        value = value.rstrip("/")
        if (
            not value.startswith("viking://")
            or any(part in {"..", ".", "archive"} for part in value[9:].split("/"))
            or any(char in value for char in "%?#\\")
        ):
            raise ValueError("Invalid Viking namespace")
        return value


class KnowledgeUpdate(KnowledgeSettings):
    clear_key: bool = False


class KnowledgeConfig:
    def __init__(self, root: Path) -> None:
        self.path = root / "knowledge" / "connection.json"

    def read(self) -> KnowledgeSettings:
        return (
            KnowledgeSettings.model_validate_json(self.path.read_text())
            if self.path.exists()
            else KnowledgeSettings()
        )

    def public(self) -> dict:
        settings = self.read()
        return settings.model_dump(exclude={"api_key"}) | {"has_api_key": bool(settings.api_key)}

    def save(self, update: KnowledgeUpdate) -> dict:
        data = update.model_dump(exclude={"api_key", "clear_key"})
        previous = self.read()
        key = update.api_key or (previous.api_key if update.endpoint == previous.endpoint else None)
        data["api_key"] = None if update.clear_key or not key else key.get_secret_value()
        write_private(self.path, data)
        return self.public()


class KnowledgeQuery(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=6, ge=1, le=50)
    scope: Literal["project", "personal", "both"] = "both"


class KnowledgeRead(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    level: str = Field(default="content", pattern="^(abstract|overview|content)$")
    scope: Literal["project", "personal"] = "project"


class KnowledgeWrite(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=300000)
    scope: Literal["project", "personal"] = "project"
