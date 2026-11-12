"""Stop only the calculation nodes recorded in one conversation plan."""

from collections import defaultdict
from uuid import UUID

from pydantic import BaseModel


class PlanStop(BaseModel):
    plan_id: UUID


def job_ids(plan: dict) -> list[str]:
    identifiers = set()
    for state in plan["runtime"].values():
        for attempt in [
            state,
            *(item["state"] for item in state.get("attempts", []) if "state" in item),
        ]:
            if attempt.get("generic_job_id"):
                continue
            identifier = attempt.get("job_id") or (attempt.get("job") or {}).get("id")
            if identifier:
                identifiers.add(str(UUID(identifier)))
    return sorted(identifiers)


def matching_jobs(scheduler, account: str, expected: dict[str, str]) -> list[str]:
    from aiida.schedulers.datastructures import JobState

    return [
        str(job.job_id)
        for job in scheduler.get_jobs(user=account)
        if job.job_state != JobState.DONE
        and str(job.job_id) in expected
        and expected[str(job.job_id)] == job.title
    ]


def stop_jobs(payload: dict) -> dict:
    from aiida import orm
    from aiida.engine.processes.control import kill_processes
    from aiida.manage import get_manager

    from chemistry_agent.automation.backend import PREFIX

    ids = set(payload["job_ids"])
    nodes = (
        orm.QueryBuilder()
        .append(orm.CalcJobNode, filters={"uuid": {"in": list(ids)}})
        .all(flat=True)
        if ids
        else []
    )
    if {n.uuid for n in nodes} != ids or any(not n.label.startswith(PREFIX) for n in nodes):
        raise ValueError("Plan contains an unknown or unmanaged calculation.")
    groups = defaultdict(list)
    for node in nodes:
        groups[node.computer.uuid].append(node)
    remote = []
    for group in groups.values():
        computer = group[0].computer
        with computer.get_transport() as transport:
            scheduler = computer.get_scheduler()
            scheduler.set_transport(transport)
            account = transport.whoami()
            expected = {str(n.get_job_id()): f"aiida-{n.pk}" for n in group if n.get_job_id()}

            for identifier in matching_jobs(scheduler, account, expected):
                scheduler.kill_job(identifier)
            remote.extend(matching_jobs(scheduler, account, expected))
    active = [n for n in nodes if not n.is_terminated]
    if active:
        kill_processes(active, msg_text="Stopped at user's request", timeout=15)
    storage = get_manager().get_profile_storage()
    if hasattr(storage, "get_session"):
        storage.get_session().commit()
    remaining = [n.pk for n in nodes if not orm.load_node(n.pk).is_terminated]
    return {
        "nodes_checked": len(nodes),
        "active_aiida": remaining,
        "active_scheduler": remote,
        "verified_stopped": not remaining and not remote,
    }
