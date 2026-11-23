"""Research outcome reports are distinct from scheduler facts and failed attempts."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import OperationContext
from chemistry_agent.platform.task_agents import current_task_agent
from chemistry_agent.store import utc_now


class StepReport(StrictModel):
    plan_id: UUID
    node_id: str
    expected_revision: int = Field(ge=1)
    status: Literal["running", "completed", "failed"]
    reason: str = Field(min_length=1, max_length=12000)
    result: dict = Field(default_factory=dict)
    accepted_jobs: list[UUID] = Field(default_factory=list)
    artifact_ids: list[UUID] = Field(default_factory=list)


async def report_step(plans, args: StepReport, context: OperationContext) -> dict:
    """Record an attributed conclusion without rewriting the underlying job outcomes."""
    session, identifier = context.session_id, str(args.plan_id)
    async with plans.lock(session, identifier):
        plan = plans.chat.load(session, "plans", identifier)
        if plan["revision"] != args.expected_revision:
            raise AgentError("plan_revision_conflict", "方案已变化，请读取最新版本。", 409)
        node = next((n for n in plan["nodes"] if n["id"] == args.node_id), None)
        if not node or node["kind"] != "task":
            raise AgentError("step_owner_conflict", "此操作只记录 Agent 负责的研究步骤。", 409)
        if args.node_id in plan.get("task_agents", {}):
            task = current_task_agent.get()
            if not task or (task.parent_id, task.plan_id, task.node_id) != (
                session, identifier, args.node_id,
            ):
                raise AgentError("step_owner_conflict", "请由已分配的任务线程报告此步骤。", 409)
        if plan["status"] in {"draft", "paused", "stopping", "stopped"}:
            raise AgentError("plan_not_active", "请先确认或恢复方案。", 409)
        state = plan["runtime"][args.node_id]
        if state["status"] == "completed":
            raise AgentError(
                "step_finished", "已完成步骤的结果已固定，请创建新步骤记录新的研究尝试。", 409
            )
        if any(plan["runtime"][dep]["status"] != "completed" for dep in node["depends_on"]):
            raise AgentError("dependency_pending", "研究步骤的上游尚未完成。", 409)
        accepted = [str(value) for value in args.accepted_jobs]
        jobs = state.get("jobs", [])
        if not set(accepted) <= set(jobs):
            raise AgentError("step_job_unknown", "结果引用了不属于此研究步骤的作业。", 422)
        records = []
        if args.status in {"completed", "failed"}:
            terminal = {"completed", "failed", "blocked", "stopped", "cancelled"}
            if any(
                child.get("parent_task") == args.node_id
                and plan["runtime"][child["id"]]["status"] not in terminal
                for child in plan["nodes"]
            ):
                raise AgentError("step_jobs_active", "关联作业尚未结束。", 409)
            records = [plans.slurm.load(session, job_id) for job_id in jobs]
            if any(not record.get("terminal") for record in records):
                raise AgentError(
                    "step_jobs_active", "关联作业尚未结束，请先结束或取消这些作业。", 409
                )
        if args.status == "completed" and jobs and not accepted:
            raise AgentError("step_evidence_missing", "请明确本步骤最终使用了哪些作业的结果。", 422)
        if args.status == "completed":
            gated = [
                record for record in records
                if record.get("settings", {}).get("neb_force_target") is not None
            ]
            selected = [record for record in gated if record["id"] in accepted]
            if gated and (
                not selected
                or any(record.get("science", {}).get("status") != "passed" for record in selected)
            ):
                raise AgentError(
                    "neb_acceptance_required",
                    "请选择已通过投影力阈值的 NEB 作业。下游继续等待。",
                    409,
                )
        for artifact_id in args.artifact_ids:
            plans.chat.load(session, "artifacts", str(artifact_id))
        report = args.model_dump(mode="json") | {"run_id": context.run_id, "at": utc_now()}
        state.setdefault("reports", []).append(report)
        state.update(
            status=args.status,
            outputs={
                "result": args.result,
                "artifact_ids": [str(value) for value in args.artifact_ids],
            },
            accepted_jobs=accepted,
        )
        if args.status != "running":
            state["completed_at"] = utc_now()
        else:
            state.pop("completed_at", None)
            # Reopen only descendants previously blocked by this task; keep their histories.
            descendants = {args.node_id}
            while True:
                expanded = descendants | {
                    n["id"] for n in plan["nodes"] if descendants.intersection(n["depends_on"])
                }
                if expanded == descendants:
                    break
                descendants = expanded
            for key in descendants - {args.node_id}:
                if plan["runtime"][key]["status"] == "blocked":
                    plan["runtime"][key].update(status="pending")
        plan.update(status="running", revision=plan["revision"] + 1, reported=False)
        plan["history"].append(
            {
                "revision": plan["revision"],
                "changed_nodes": [args.node_id],
                "removed_nodes": [],
                "action": "report",
                "at": utc_now(),
            }
        )
        plans.save(session, plan)
        return {
            "plan_id": identifier,
            "node_id": args.node_id,
            "revision": plan["revision"],
            "status": args.status,
            "accepted_jobs": accepted,
        }
