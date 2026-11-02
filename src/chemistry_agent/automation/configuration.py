"""User-owned calculation recipes and approved POTCAR sources, separate from model memory."""

import json
from pathlib import Path
from uuid import UUID

from filelock import FileLock
from pydantic import Field, field_validator

from chemistry_agent.automation.models import Name, PreviewRequest, Resources, StrictModel
from chemistry_agent.errors import AgentError


class PotentialSource(StrictModel):
    id: Name
    server_id: UUID
    root: str = Field(min_length=2, max_length=500)
    family: Name
    functional: str = Field(default="PBE", pattern="^PBE$")

    @field_validator("root")
    @classmethod
    def safe_root(cls, value: str) -> str:
        if not value.startswith("/") or any(c in value for c in "\n\r\x00"):
            raise ValueError("赝势库须为服务器上的绝对目录。")
        if "archive" in Path(value).parts or ".." in Path(value).parts:
            raise ValueError("不允许 archive 或上级目录。")
        return value.rstrip("/")


class QueueFallback(StrictModel):
    source: Name
    target: Name


class CalculationProtocol(StrictModel):
    quota_fallback: QueueFallback | None = None
    id: Name
    description: str = Field(min_length=1, max_length=1000)
    server_id: UUID
    code_id: UUID
    resources: Resources
    potential_source_id: Name
    potential_mapping: dict[str, Name] = Field(default_factory=dict, max_length=100)
    incar: dict = Field(min_length=1, max_length=200)
    kmesh: tuple[int, int, int]
    kpoint_spacing: float | None = Field(default=None, gt=0, le=2)
    max_atoms: int = Field(ge=1, le=10000)
    allowed_elements: list[str] = Field(default_factory=list, max_length=100)
    default: bool = False

    @field_validator("kmesh")
    @classmethod
    def valid_mesh(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        return PreviewRequest.validate_kmesh(value)

    @field_validator("potential_mapping")
    @classmethod
    def safe_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        if "archive" in value.values():
            raise ValueError("不能从 archive 导入赝势。")
        return value


class CalculationConfiguration:
    def __init__(self, root: Path):
        self.path = root / "calculation-configuration.json"

    def read(self) -> dict:
        if not self.path.exists():
            return {"sources": [], "protocols": []}
        return json.loads(self.path.read_text())

    def save(self, kind: str, value: PotentialSource | CalculationProtocol) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            data = self.read()
            item = value.model_dump(mode="json")
            if kind == "protocols" and item["default"]:
                for protocol in data[kind]:
                    protocol["default"] = False
            data[kind] = [entry for entry in data[kind] if entry["id"] != item["id"]] + [item]
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            temporary.chmod(0o600)
            temporary.replace(self.path)
        return item

    def get(self, kind: str, identifier: str) -> dict:
        for item in self.read()[kind]:
            if item["id"] == identifier:
                return item
        raise AgentError(
            "configuration_missing", "未找到计算方案或赝势源，请先在自动化设置中注册。", 422
        )
