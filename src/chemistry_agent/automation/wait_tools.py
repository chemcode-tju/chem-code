"""Agent tool descriptions; all operations delegate to the computation platform."""

from chemistry_agent.automation.wait_models import WaitControl, WaitRegister
from chemistry_agent.tools import Tool


def tools(plans) -> list[Tool]:
    return [
        Tool(
            "calculation_wait_register",
            "Persist a one-shot wait in the current plan/session. Compose all/any conditions: "
            "state(node,states) or file(node,pattern). On match, wake this agent with instruction. "
            "Use a stable key for idempotency; new key for a new wait. replace_default groups "
            "covered nodes instead of waking on every completion. No new compute authority. "
            "File conditions observe files, not scientific validity or write completeness.",
            WaitRegister,
            plans.platform.handler("wait.register"),
            True,
        ),
        Tool(
            "calculation_wait_control",
            "List durable waits/event recovery status, cancel a wait, "
            "or retry a blocked model boundary after fixing configuration. "
            "Never replays uncertain tools.",
            WaitControl,
            plans.platform.handler("wait.control"),
            True,
        ),
    ]
