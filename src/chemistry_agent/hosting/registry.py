"""Platform-owned locations. Native credentials never enter this registry."""

import json
import platform
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from chemistry_agent.automation.ssh_profiles import write_private
from chemistry_agent.environments.registry import Environments
from chemistry_agent.errors import AgentError


def validate_directory(value: str) -> str:
    """Accept an optional working folder without changing its filesystem."""
    value = value.strip()
    if value and not value.startswith(("/", "~/")):
        raise ValueError("文件夹须为绝对路径或 ~/ 开头的路径")
    if "archive" in Path(value).parts or "\x00" in value or ".." in Path(value).parts:
        raise ValueError("不能使用 archive 或父目录跳转")
    if value == "~/":
        return value
    return value.rstrip("/") or ("/" if value else "")


class AgentInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    name: str = Field(min_length=1, max_length=160)
    host_id: str = "local"
    backend: Literal["builtin", "codex", "antigravity"]
    executable: str = Field(default="", max_length=1024)
    model: str = Field(default="", max_length=160)
    effort: str = ""
    enabled: bool = True


class Hosting:
    def __init__(self, root: Path, defaults: tuple[str, ...] = ()) -> None:
        self.root, self.defaults = root / "hosting", defaults
        self.environments = Environments(root)

    def data(self) -> dict:
        path = self.root / "registry.json"
        return json.loads(path.read_text()) if path.exists() else {"agents": []}

    def installation(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "registry.lock")):
            data = self.data()
            if not data.get("installation"):
                data["installation"] = str(uuid4())
                write_private(self.root / "registry.json", data)
            return data["installation"]

    def hosts(self) -> list[dict]:
        # 'local' always means the platform host, not the browser's computer.
        rows = [
            {
                "id": "local",
                "name": f"本机 · {platform.node()}",
                "transport": "local",
                "enabled": True,
            }
        ]
        rows.extend(
            row
            for row in self.environments.inventory(self.defaults)
            if row["id"] != "local" and row["transport"] == "ssh"
        )
        return rows

    def host(self, identifier: str) -> dict:
        row = next((item for item in self.hosts() if item["id"] == identifier), None)
        if not row or not row["enabled"]:
            raise AgentError("agent_host_unavailable", "请先在服务器与环境中启用此主机。", 409)
        return row

    def agents(self, config) -> list[dict]:
        defaults = [
            AgentInstance(
                id=f"local-{backend}",
                name=name,
                backend=backend,
                executable=getattr(config, f"{backend}_executable", ""),
                model=getattr(config, f"{backend}_model", ""),
                effort=getattr(config, f"{backend}_effort", ""),
            ).model_dump()
            for backend, name in (
                ("builtin", "Chem Agent"),
                ("codex", "Codex"),
                ("antigravity", "Antigravity"),
            )
        ]
        saved = {item["id"]: item for item in self.data()["agents"]}
        return [saved.pop(item["id"], item) for item in defaults] + list(saved.values())

    def save(self, item: AgentInstance) -> dict:
        self.host(item.host_id)
        if item.backend == "builtin" and item.host_id != "local":
            raise AgentError(
                "agent_host_unsupported",
                "Chem Agent 随平台运行；远端可添加 Codex 或 Antigravity。",
                422,
            )
        if item.backend != "builtin" and not item.executable.strip():
            raise AgentError("agent_path_required", "请填写 Agent 程序路径。", 422)
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "registry.lock")):
            data = self.data()
            rows = data["agents"]
            data["agents"] = (
                [item.model_dump() if row["id"] == item.id else row for row in rows]
                if any(row["id"] == item.id for row in rows)
                else [*rows, item.model_dump()]
            )
            write_private(self.root / "registry.json", data)
        return item.model_dump()

    def resolve(self, choice, config) -> dict:
        identifier = choice.instance_id or f"local-{choice.backend}"
        instance = next((a for a in self.agents(config) if a["id"] == identifier), None)
        if not instance or not instance["enabled"] or instance["backend"] != choice.backend:
            raise AgentError("agent_instance_missing", "Agent 实例不存在或未启用。", 409)
        self.host(instance["host_id"])
        monitor = choice.monitor_host or "auto"
        if monitor != "auto":
            self.host(monitor)
        return {
            "instance_id": identifier,
            "host_id": instance["host_id"],
            "monitor_host": monitor,
            **(
                {f"{choice.backend}_executable": instance["executable"]}
                if choice.backend != "builtin"
                else {}
            ),
        }
