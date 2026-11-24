"""A team member can transfer one bounded assignment to another enrolled member."""

from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import Field

from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import StrictModel


class HandoffInput(StrictModel):
    target: str = Field(min_length=1, max_length=96, description="Target member ID or exact name")
    task: str = Field(
        min_length=1, max_length=2000, description="Work requested and evidence needed"
    )


@dataclass
class HandoffScope:
    targets: dict[str, str]
    decision: dict | None = None


active_handoff: ContextVar[HandoffScope | None] = ContextVar("active_handoff", default=None)


async def handoff(args: HandoffInput) -> dict:
    scope = active_handoff.get()
    if scope is None:
        raise AgentError("group_required", "当前回合未启用交接。", 409)
    matches = [key for key, name in scope.targets.items() if args.target in {key, name}]
    if len(matches) != 1:
        raise AgentError("invalid_member", "请选择另一位群组成员，重名时使用成员 ID。", 422)
    decision = {"target": matches[0], "name": scope.targets[matches[0]], "task": args.task}
    if scope.decision and scope.decision != decision:
        raise AgentError("handoff_selected", "本次交接已有接手人。", 409)
    scope.decision = decision
    return decision
