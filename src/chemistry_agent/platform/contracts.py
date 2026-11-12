"""Ports shared by browser, Python clients and optional agent integrations."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

from chemistry_agent.schemas import TokenUsage


@dataclass(frozen=True)
class OperationContext:
    session_id: str
    run_id: str


class DeliveryAdapter(Protocol):
    """Handle a claimed event; the dispatcher owns claim, retry and atomic acknowledgement.

    Tool-using consumers use the supplied checkpoint context before external effects.
    A repeated offer is not permission to repeat an unknown external side effect.
    """

    def available(self) -> bool: ...

    async def handle(self, delivery: "Delivery") -> "DeliveryResult": ...


@dataclass
class Delivery:
    session_id: str
    plan: dict
    event: dict
    payload: dict
    run_id: str
    history: list[dict]
    owner_id: str | None
    tool_calls: list = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    context: object | None = None


@dataclass
class DeliveryResult:
    answer: str
    messages: list[dict] = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)


job_event_target: ContextVar[tuple[str, str] | None] = ContextVar("job_event_target", default=None)
