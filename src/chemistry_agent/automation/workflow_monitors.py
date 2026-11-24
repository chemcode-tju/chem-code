"""Read compact monitor projections from the platform's existing plan files and wait ledger."""

import asyncio
import json
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from chemistry_agent.store import Store


@lru_cache(maxsize=256)
def project_plan(path: Path, signature: tuple[int, int, int]) -> dict:
    """Atomic replacements invalidate the cache even when two revisions share an mtime."""
    plan = json.loads(path.read_text())
    nodes = []
    for node in plan["nodes"]:
        runtime = plan.get("runtime", {}).get(node["id"], {})
        job = runtime.get("job") or {}
        nodes.append(
            {
                "id": node["id"],
                "title": node.get("title") or node["id"],
                "status": runtime.get("status"),
                "scheduler_state": job.get("scheduler_state"),
                "scheduler_job_id": job.get("scheduler_job_id"),
            }
        )
    return {
        "id": plan["id"],
        "title": plan.get("title") or plan["id"],
        "status": plan["status"],
        "updated_at": plan.get("updated_at") or plan.get("created_at") or "",
        "nodes": nodes,
    }


def read_plans(root: Path, sessions: dict[str, str]) -> tuple[list[dict], int]:
    records = []
    unreadable = 0
    for session, title in sessions.items():
        directory = root / session / "plans"
        if not directory.is_dir() or directory.is_symlink() or directory.parent.is_symlink():
            continue
        for path in directory.glob("*.json"):
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
                plan = project_plan(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError):
                unreadable += 1
                continue
            records.append(
                {
                    **plan,
                    "session_id": session,
                    "session_title": title,
                    "waits": [],
                    "deliveries": {},
                }
            )
    return sorted(records, key=lambda p: p["updated_at"], reverse=True), unreadable


async def workflows(store: Store, root: Path, session: UUID | None) -> dict:
    scope = str(session) if session else None
    async with store.lock:
        async with store.db.execute(
            "SELECT id,title FROM sessions WHERE owner_id IS NULL "
            "AND id NOT IN (SELECT session_id FROM task_agent_sessions) AND (? IS NULL OR id=?)",
            (scope, scope),
        ) as cursor:
            sessions = dict(await cursor.fetchall())
        async with store.db.execute(
            "SELECT w.session_id,w.plan_id,w.spec,w.status,e.status AS delivery FROM agent_waits w "
            "JOIN sessions s ON w.session_id=s.id LEFT JOIN agent_events e ON e.id=w.event_id "
            "WHERE s.owner_id IS NULL AND (? IS NULL OR w.session_id=?) ORDER BY w.created_at DESC",
            (scope, scope),
        ) as cursor:
            waits = await cursor.fetchall()
        async with store.db.execute(
            "SELECT e.session_id,e.plan_id,e.status,count(*) AS count FROM agent_events e "
            "JOIN sessions s ON e.session_id=s.id WHERE s.owner_id IS NULL "
            "AND (? IS NULL OR e.session_id=?) GROUP BY e.session_id,e.plan_id,e.status",
            (scope, scope),
        ) as cursor:
            deliveries = await cursor.fetchall()
    records, unreadable = await asyncio.to_thread(read_plans, root, sessions)
    by_plan = {(record["session_id"], record["id"]): record for record in records}
    for wait in waits:
        record = by_plan.get((wait["session_id"], wait["plan_id"]))
        if record:
            spec = json.loads(wait["spec"])
            record["waits"].append(
                {
                    "key": spec["key"],
                    "instruction": spec["instruction"],
                    "condition": spec["condition"],
                    "status": wait["status"],
                    "delivery": wait["delivery"],
                }
            )
    for delivery in deliveries:
        record = by_plan.get((delivery["session_id"], delivery["plan_id"]))
        if record:
            record["deliveries"][delivery["status"]] = delivery["count"]
    return {"plans": records, "unreadable": unreadable}
