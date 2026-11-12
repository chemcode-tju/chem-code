"""Declarative observations, not a catalogue of scientific workflows."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from chemistry_agent.automation.models import StrictModel


class WaitCondition(StrictModel):
    op: Literal["all", "any", "state", "file"]
    conditions: list["WaitCondition"] = Field(default_factory=list)
    node: str | None = None
    states: list[str] = Field(default_factory=list)
    pattern: str | None = None

    @model_validator(mode="after")
    def valid_expression(self) -> "WaitCondition":
        if self.op in {"all", "any"}:
            if not self.conditions or self.node or self.states or self.pattern:
                raise ValueError("all/any require nonempty conditions only")
        elif self.conditions or not self.node:
            raise ValueError("state/file require a node and no child conditions")
        elif self.op == "state" and (not self.states or self.pattern):
            raise ValueError("state requires states")
        elif self.op == "file" and (not self.pattern or self.states):
            raise ValueError("file requires an output-relative glob pattern")
        return self

    def nodes(self) -> set[str]:
        return {self.node} if self.node else set().union(*(c.nodes() for c in self.conditions))


class WaitRegister(StrictModel):
    plan_id: UUID
    key: str = Field(min_length=1, max_length=160)
    condition: WaitCondition
    instruction: str = Field(min_length=1, max_length=12000)
    replace_default: bool = True
    poll_seconds: float = Field(default=30, ge=1)
    expires_at: float | None = Field(default=None, gt=0, description="Optional Unix timestamp")


class WaitControl(StrictModel):
    plan_id: UUID
    action: Literal["list", "cancel", "retry"] = "list"
    wait_id: UUID | None = None
    event_id: UUID | None = None

    @model_validator(mode="after")
    def valid_target(self) -> "WaitControl":
        if self.action != "list" and bool(self.wait_id) == bool(self.event_id):
            raise ValueError("cancel/retry require exactly one of wait_id or event_id")
        return self
