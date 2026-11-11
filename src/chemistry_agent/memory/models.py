"""Small, portable research documents; scientific trust is assigned by collectors."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["plan", "calculation", "lesson", "document"] = "document"
    content: str = Field(min_length=1, max_length=100000)
    systems: list[str] = Field(default_factory=list, max_length=30)
    tasks: list[str] = Field(default_factory=list, max_length=30)
    parameters: dict = Field(default_factory=dict)
    applicability: str = Field(default="", max_length=4000)


class MemorySave(MemoryDocument):
    memory_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)


class MemorySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=2000)
    scope: Literal["session", "project", "personal"] = "project"
    kind: Literal["plan", "calculation", "lesson", "document"] | None = None
    limit: int = Field(default=6, ge=1, le=20)
    offset: int = Field(default=0, ge=0)


class MemoryRead(BaseModel):
    memory_id: UUID
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=20000)


class MemoryCompare(BaseModel):
    memory_id: UUID
    parameters: dict


class MemoryForget(BaseModel):
    expected_version: int = Field(ge=1)


def flatten(values: dict, prefix: str = "") -> dict:
    """Keep lists intact (k meshes, spin vectors); normalize only INCAR key case."""
    result = {}
    for key, value in values.items():
        key = key.upper() if prefix == "incar" else key
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def parameter_diff(reference: dict, current: dict) -> list[dict]:
    """Missing is unknown, never an implicit default or an inferred error cause."""
    left, right = flatten(reference), flatten(current)
    return [
        {
            "parameter": key,
            "reference": left.get(key),
            "current": right.get(key),
            "reference_present": key in left,
            "current_present": key in right,
        }
        for key in sorted(left.keys() | right.keys())
        if key not in left or key not in right or left[key] != right[key]
    ]
