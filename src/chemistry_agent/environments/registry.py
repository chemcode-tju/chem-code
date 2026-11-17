"""Local/SSH/HTTP execution environments with explicit, private connection references."""

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from chemistry_agent.automation.ssh_profiles import write_private
from chemistry_agent.errors import AgentError


class Environment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    transport: Literal["local", "runner"] = "runner"
    endpoint: str = "http://127.0.0.1:8767"
    token: SecretStr | None = None
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Runner requires an HTTP(S) endpoint without embedded credentials")
        return value.rstrip("/")


class Environments:
    def __init__(self, root: Path, transport=None) -> None:
        self.root = root / "environments"
        self.path = self.root / "registry.json"
        self.transport = transport

    def records(self) -> list[Environment]:
        data = json.loads(self.path.read_text()) if self.path.exists() else {"environments": []}
        return [Environment.model_validate(item) for item in data["environments"]]

    def get(self, host: str) -> Environment | None:
        return next((e for e in self.records() if e.id == host), None)

    def hosts(self) -> list[str]:
        return [e.id for e in self.records() if e.enabled]

    def public(self) -> list[dict]:
        return [
            e.model_dump(exclude={"token"}) | {"has_token": bool(e.token)} for e in self.records()
        ]

    def save(self, value: Environment) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "registry.lock")):
            records = self.records()
            old = next((e for e in records if e.id == value.id), None)
            if old and old.endpoint == value.endpoint and not value.token:
                value.token = old.token
            encoded = []
            for e in [*(e for e in records if e.id != value.id), value]:
                encoded.append(
                    e.model_dump(exclude={"token"})
                    | {"token": e.token.get_secret_value() if e.token else None}
                )
            write_private(self.path, {"environments": encoded})
        return value.model_dump(exclude={"token"}) | {"has_token": bool(value.token)}

    async def request(self, host: str, operation: str, payload: dict | None = None) -> dict:
        environment = self.get(host)
        if not environment or not environment.enabled:
            raise AgentError("environment_disabled", "执行环境未启用。", 409)
        if environment.transport == "local":
            from chemistry_agent.environments.runner import RunnerOperations

            ops = RunnerOperations(self.root / f"local-{environment.id}")
            return await ops.execute(operation, payload or {})
        if not environment.token:
            raise AgentError("runner_token_missing", "请配置 Runner 连接令牌。", 409)
        try:
            async with httpx.AsyncClient(
                base_url=environment.endpoint,
                timeout=1800 if operation == "slurm" else 65,
                headers={"Authorization": f"Bearer {environment.token.get_secret_value()}"},
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(f"/v1/{operation}", json=payload or {})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentError(
                "runner_unavailable", "Runner 不可达；操作结果未确认，未自动重发。", 503
            ) from exc

    async def slurm(self, host: str, payload: dict) -> dict:
        result = await self.request(host, "slurm", payload)
        if result.get("error"):
            raise AgentError("job_remote_error", result["error"], 502)
        return result
