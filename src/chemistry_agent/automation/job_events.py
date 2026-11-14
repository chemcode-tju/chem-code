"""Legacy import path for neutral event projection and the chat delivery adapter."""

from chemistry_agent.automation.wait_ledger import evidence as node_evidence
from chemistry_agent.platform.contracts import job_event_target as job_event_target


def record_events(plan: dict) -> None:
    events = plan.setdefault("job_events", {})
    for node in plan["nodes"]:
        state = plan["runtime"][node["id"]]
        if node["kind"] == "task":
            if state["status"] == "pending" and all(
                plan["runtime"][dep]["status"] == "completed" for dep in node["depends_on"]
            ):
                key = f"ready:{node['id']}:{len(state.get('attempts', []))}"
                events.setdefault(
                    key,
                    {
                        "status": "pending",
                        "node_id": node["id"],
                        "outcome": "ready",
                        "evidence": {
                            **node_evidence(plan, node["id"]),
                            "type": "step_ready",
                            "instruction": node.get("purpose"),
                        },
                    },
                )
            continue
        if node["kind"] != "job" or state["status"] not in {"completed", "failed"}:
            continue
        key = state.get("generic_job_id") or f"{node['id']}:{len(state.get('attempts', []))}"
        events.setdefault(
            key,
            {
                "status": "pending",
                "node_id": node["id"],
                "outcome": state["status"],
                "evidence": node_evidence(plan, node["id"]),
            },
        )


async def wake(plans, session: str, identifier: str) -> None:
    await plans.dispatcher.deliver_now(session, identifier, "chat")
