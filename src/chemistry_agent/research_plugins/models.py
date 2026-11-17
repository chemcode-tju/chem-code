"""Validated inputs shared by the daemon and both MCP plugins."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from chemistry_agent.automation.models import StrictModel


class Scope(StrictModel):
    workspace_id: UUID


class JobRegister(Scope):
    key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    backend: Literal["simulated", "slurm"] = "simulated"
    host: str = Field(default="", pattern=r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")
    scheduler_id: str = Field(default="", pattern=r"^([0-9]+(_[0-9]+)?)?$")
    duration_seconds: float = Field(default=5, ge=0, le=86400, allow_inf_nan=False)
    outcome: Literal["COMPLETED", "FAILED", "CANCELLED"] = "COMPLETED"
    plan_id: UUID | None = None
    node_id: str | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "JobRegister":
        if self.backend == "slurm" and (not self.host or not self.scheduler_id):
            raise ValueError("Slurm 作业需要 host 和 scheduler_id")
        if bool(self.plan_id) != bool(self.node_id):
            raise ValueError("plan_id 与 node_id 必须同时指定")
        return self


class WaitRegister(Scope):
    thread_id: UUID
    job_ids: list[UUID] = Field(min_length=1, max_length=100)
    mode: Literal["all", "any"] = "all"


class WaitRead(Scope):
    wait_id: UUID
    timeout_seconds: float = Field(default=0, ge=0, le=30, allow_inf_nan=False)


class WaitControl(Scope):
    wait_id: UUID
    action: Literal["acknowledge", "cancel"]
    event_id: UUID | None = None


class FolderLink(Scope):
    plan_id: UUID
    node_id: str
    path: str = Field(min_length=1, max_length=4096)


class FolderRead(Scope):
    plan_id: UUID
    node_id: str
    path: str = Field(
        default="",
        max_length=4096,
        description="Project-relative path; empty opens the step directory, '.' the project root.",
    )
    offset: int = Field(default=0, ge=0)
