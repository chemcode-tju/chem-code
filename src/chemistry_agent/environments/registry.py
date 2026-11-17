"""Local/SSH/HTTP execution environments with explicit, private connection references."""

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from chemistry_agent.automation.local_ssh import discover_aliases, enabled_aliases
from chemistry_agent.automation.ssh_profiles import load_profile, shell_profiles, write_private
from chemistry_agent.errors import AgentError


class Environment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    transport: Literal["local", "runner", "ssh"] = "runner"
    ssh_alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    endpoint: str = "http://127.0.0.1:8767"
    token: SecretStr | None = None
    enabled: bool = True
    removed: bool = False
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
        self.platform_root = root
        self.root = root / "environments"
        self.path = self.root / "registry.json"
        self.transport = transport

    def records(self) -> list[Environment]:
        data = json.loads(self.path.read_text()) if self.path.exists() else {"environments": []}
        return [Environment.model_validate(item) for item in data["environments"]]

    def get(self, host: str) -> Environment | None:
        return next((e for e in self.records() if e.id == host), None)

    def hosts(self) -> list[str]:
        return [e.id for e in self.records() if e.enabled and not e.removed]

    def ssh_choices(self, defaults: tuple[str, ...] = ()) -> list[dict]:
        choices = {row["alias"]: row for row in discover_aliases()}
        for alias, identifier in shell_profiles(self.platform_root).items():
            profile = load_profile(self.platform_root, identifier)
            choices.setdefault(
                alias,
                {
                    "alias": alias,
                    "source": "platform SSH",
                    **{k: profile[k] for k in ("hostname", "user", "port") if k in profile},
                },
            )
        for alias in enabled_aliases(self.platform_root, defaults):
            choices.setdefault(alias, {"alias": alias, "source": "service configuration"})
        return sorted(choices.values(), key=lambda row: row["alias"])

    def inventory(self, defaults: tuple[str, ...] = ()) -> list[dict]:
        """Project legacy SSH entries into one list without changing existing grants."""
        records = self.records()
        claimed = {e.id for e in records} | {e.ssh_alias for e in records if e.ssh_alias}
        choices = {row["alias"]: row for row in self.ssh_choices(defaults)}
        legacy = set(enabled_aliases(self.platform_root, defaults)) | set(
            shell_profiles(self.platform_root)
        )
        rows = self.public()
        rows.extend(
            Environment(id=alias, name=alias, transport="ssh", ssh_alias=alias).model_dump(
                exclude={"token"}
            )
            | {"has_token": False}
            for alias in sorted(legacy - claimed)
        )
        for row in rows:
            if row["transport"] == "ssh":
                row["ssh"] = choices.get(row.get("ssh_alias") or row["id"], {})
        return rows

    def available_hosts(self, defaults: tuple[str, ...] = ()) -> list[str]:
        records = self.records()
        claimed = {e.id for e in records} | {e.ssh_alias for e in records if e.ssh_alias}
        legacy = set(enabled_aliases(self.platform_root, defaults)) | set(
            shell_profiles(self.platform_root)
        )
        return sorted((legacy - claimed) | set(self.hosts()))

    def public(self) -> list[dict]:
        return [
            e.model_dump(exclude={"token"}) | {"has_token": bool(e.token)}
            for e in self.records()
            if not e.removed
        ]

    def save(
        self, value: Environment, *, create: bool = False, defaults: tuple[str, ...] = ()
    ) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "registry.lock")):
            records = self.records()
            old = next((e for e in records if e.id == value.id), None)
            if create and any(row["id"] == value.id for row in self.inventory(defaults)):
                raise AgentError(
                    "environment_exists", "节点已存在，请在原节点中修改连接方式。", 409
                )
            if value.transport == "ssh" and not value.removed:
                alias = value.ssh_alias or value.id
                if alias not in {row["alias"] for row in self.ssh_choices(defaults)}:
                    raise AgentError("ssh_alias_unknown", "请选择平台 SSH 配置中已有的主机。", 422)
                if any(
                    e.id != value.id and not e.removed and (e.ssh_alias or e.id) == alias
                    for e in records
                ):
                    raise AgentError("environment_exists", "该 SSH 主机已在另一个节点中配置。", 409)
                value.ssh_alias = alias
            if old and old.endpoint == value.endpoint and not value.token:
                value.token = old.token
            encoded = []
            updated = (
                [value if e.id == value.id else e for e in records] if old else [*records, value]
            )
            for e in updated:
                encoded.append(
                    e.model_dump(exclude={"token"})
                    | {"token": e.token.get_secret_value() if e.token else None}
                )
            write_private(self.path, {"environments": encoded})
        return value.model_dump(exclude={"token"}) | {"has_token": bool(value.token)}

    def remove(self, identifier: str, defaults: tuple[str, ...] = ()) -> None:
        record = next((row for row in self.inventory(defaults) if row["id"] == identifier), None)
        if record is None:
            raise AgentError("environment_missing", "节点不存在。", 404)
        value = self.get(identifier) or Environment.model_validate(
            {k: v for k, v in record.items() if k in Environment.model_fields}
        )
        # Retain a tombstone: removing a Runner must never resurrect an old SSH grant.
        self.save(value.model_copy(update={"enabled": False, "removed": True}), defaults=defaults)

    async def request(self, host: str, operation: str, payload: dict | None = None) -> dict:
        environment = self.get(host)
        if not environment or not environment.enabled or environment.removed:
            raise AgentError("environment_disabled", "执行环境未启用。", 409)
        if environment.transport == "ssh":
            raise AgentError("environment_transport", "SSH 节点使用原有 SSH 通道。", 422)
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
