"""Project unresolved actions from task threads and current job attempts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from chemistry_agent.errors import AgentError

if TYPE_CHECKING:
    from chemistry_agent.automation.plans import CalculationPlans


def job_record(plans: CalculationPlans, session: str, identifier: str) -> dict | None:
    try:
        return plans.slurm.load(session, identifier)
    except AgentError as exc:
        if exc.status_code != 404:
            raise
        return None


def job_facts(record: dict) -> dict:
    return {
        "job_id": record["id"],
        "host": record["host"],
        "scheduler_job_id": record.get("scheduler_job_id"),
        "scheduler_state": record.get("scheduler_state"),
        "remote_directory": record.get("remote_directory"),
        "submission_name": record.get("name") or f"chem-{record['id']}",
        "checked_at": record.get("checked_at"),
        "submission_status": record["status"],
    }


async def pending_actions(plans: CalculationPlans, session: str, plan: dict) -> list[dict]:
    if plan["status"] in {"draft", "stopping", "stopped", "completed"}:
        return []
    nodes = {node["id"]: node for node in plan["nodes"]}
    async with plans.store.lock, plans.store.db.execute(
        "SELECT t.node_id,a.id,a.payload FROM task_agent_sessions t "
        "JOIN agent_approvals a USING (session_id) "
        "WHERE t.parent_id=? AND t.plan_id=? AND a.state='pending' ORDER BY a.rowid",
        (session, plan["id"]),
    ) as cursor:
        rows = await cursor.fetchall()
    items = []
    for row in rows:
        node_id = row["node_id"]
        if node_id not in nodes or node_id not in plan.get("task_agents", {}):
            continue
        payload = json.loads(row["payload"])
        details = payload.get("details")
        items.append(
            {
                "kind": "approval",
                "node_id": node_id,
                "title": nodes[node_id]["title"],
                "approval_id": row["id"],
                "request_title": (details.get("title") if isinstance(details, dict) else None)
                or payload.get("title"),
            }
        )
    by_owner: dict[str, list[dict]] = {}
    for node in nodes.values():
        state = plan["runtime"][node["id"]]
        if identifier := state.get("generic_job_id"):
            owner = node.get("parent_task") or node["id"]
            by_owner.setdefault(owner, []).append(state)
            if state["status"] not in {"preparing", "running"} or state.get("job", {}).get(
                "scheduler_job_id"
            ):
                continue
            record = job_record(plans, session, identifier)
            if record and record["status"] == "uncertain" and not record.get("scheduler_job_id"):
                items.append(
                    {
                        "kind": "submission",
                        "node_id": owner,
                        "title": nodes[owner]["title"],
                        "job_title": node["title"],
                        **job_facts(record),
                    }
                )
    for owner, attempts in by_owner.items():
        if plan["runtime"][owner]["status"] == "completed":
            continue
        # Prefer the latest scientific attempt and retire its alarm when a new job is active.
        candidates = [state for state in attempts if state.get("job", {}).get("science")]
        if not candidates or any(
            state["status"] in {"preparing", "running"} for state in attempts
        ):
            continue
        latest = candidates[-1]["job"]
        if latest["science"]["status"] != "passed":
            items.append(
                {
                    "kind": "science",
                    "node_id": owner,
                    "title": nodes[owner]["title"],
                    "science": latest["science"],
                    **job_facts(latest),
                }
            )
    return items


async def reconcile_job(
    plans: CalculationPlans, session: str, plan_id: UUID, node_id: str, job_id: UUID
) -> dict:
    """Authorize the exact plan attempt and query Slurm without executing the plan."""
    identifier = str(job_id)
    plan = plans.chat.load(session, "plans", str(plan_id))
    if not any(
        node_id in {node["id"], node.get("parent_task")}
        and plan["runtime"][node["id"]].get("generic_job_id") == identifier
        for node in plan["nodes"]
    ):
        raise AgentError("step_job_mismatch", "此作业不属于当前步骤的有效尝试。", 404)
    # The immutable attempt belongs to this session even if the plan advances
    # while Slurm is replying. Its readback does not hold the plan lock.
    record = await plans.slurm.reconcile_submission(session, identifier)
    return job_facts(record)
