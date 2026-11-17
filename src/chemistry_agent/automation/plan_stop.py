"""Stop only the calculation nodes recorded in one conversation plan."""

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
