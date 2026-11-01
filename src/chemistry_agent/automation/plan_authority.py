"""Authority comes from the current user message, never from generated code/tool output."""

import re
from contextvars import ContextVar

auto_queue: ContextVar[str | None] = ContextVar("auto_plan_queue", default=None)
auto_request: ContextVar[str | None] = ContextVar("auto_plan_request", default=None)
repair_target: ContextVar[tuple[str, str] | None] = ContextVar("repair_target", default=None)


def authorized_queue(message: str) -> str | None:
    """An explicit /auto QUEUE header authorizes this turn, not subsequent messages."""
    header = message.strip().partition("\n")[0].strip()
    match = re.fullmatch(r"/auto\s+([A-Za-z0-9_.-]+)", header)
    return match.group(1) if match else None
