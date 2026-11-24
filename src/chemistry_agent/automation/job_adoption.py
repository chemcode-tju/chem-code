"""Bind an externally submitted Slurm allocation to an application conversation."""

import asyncio
import json
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from chemistry_agent.automation.job_models import JobAdopt, JobSettings
from chemistry_agent.automation.plan_authority import auto_queue
from chemistry_agent.automation.plan_models import PlanNode
from chemistry_agent.errors import AgentError
from chemistry_agent.store import utc_now
from chemistry_agent.tools import ToolContext


async def adopt_job(plans, args: JobAdopt, context: ToolContext) -> dict:
    """Registration is read-only remotely; retries repair only the local binding."""
    session = context.session_id
    if args.step_id and not args.plan_id:
        raise AgentError("plan_required", "关联研究步骤须提供 plan_id。", 422)
    observed = await plans.slurm.request(
        args.host,
        {
            "root": ".",
            "action": "observe_external",
            "scheduler_job_id": args.scheduler_job_id,
            "submitted_at": args.submitted_at,
        },
    )
    if not observed.get("known"):
        raise AgentError(
            "job_identity_unknown", observed.get("sync_error", "无法确认已有作业。"), 409
        )
    directory = observed["remote_directory"]
    if "archive" in PurePosixPath(directory).parts:
        raise AgentError("job_directory_denied", "不接管 archive 中的文件。", 422)
    if args.remote_directory and args.remote_directory.rstrip("/") != directory.rstrip("/"):
        raise AgentError("job_directory_mismatch", "提供的目录与调度器 WorkDir 不符。", 409)
    identity = json.dumps(observed["identity"], sort_keys=True)
    identifier = str(uuid5(NAMESPACE_URL, f"adopt:{session}:{args.host}:{identity}"))
    async with plans.slurm.locks.setdefault(identifier, asyncio.Lock()):
        # Do not duplicate an already managed job in this same conversation.
        for existing in plans.chat.list_records(session, "slurm_jobs"):
            if (
                existing.get("host") == args.host
                and existing.get("scheduler_job_id") == args.scheduler_job_id
                and existing.get("remote_directory") == directory
                and existing.get("origin") != "adopted"
            ):
                for plan in plans.chat.list_records(session, "plans"):
                    for node, state in plan["runtime"].items():
                        if state.get("generic_job_id") == existing["id"]:
                            await plans.platform.results.ensure_job(
                                session,
                                plan["id"],
                                node,
                                existing["id"],
                                args.results,
                                existing.get("step_id"),
                                replace=args.results != "auto",
                            )
                            return binding(existing, plan["id"], node, reused=True)
        receipt = plans.chat.directory(session, "slurm_jobs") / f"{identifier}.json"
        old = plans.slurm.load(session, identifier) if receipt.exists() else None
        plan_id = old["plan_id"] if old else str(args.plan_id or uuid5(NAMESPACE_URL, identifier))
        node_id = old["node_id"] if old else f"observed_{identifier.replace('-', '')[:16]}"
        async with plans.lock(session, plan_id):
            path = plans.chat.directory(session, "plans") / f"{plan_id}.json"
            plan = plans.chat.load(session, "plans", plan_id) if path.exists() else None
            if plan and node_id in plan["runtime"]:
                await plans.platform.results.ensure_job(
                    session,
                    plan_id,
                    node_id,
                    identifier,
                    args.results
                    if args.results != "auto"
                    else old.get("settings", {}).get("results", "auto"),
                    old.get("step_id"),
                    replace=args.results != "auto",
                )
                return binding(old, plan_id, node_id, reused=True)
            if plan and plan["status"] in {"draft", "paused", "stopping", "stopped"}:
                raise AgentError(
                    "plan_not_active", "请恢复方案，或省略 plan_id 创建独立监控方案。", 409
                )
            if args.plan_id and plan is None:
                raise AgentError("plan_not_found", "当前会话不存在指定方案。", 404)
            step_id = old.get("step_id") if old else args.step_id
            parent = next((n for n in plan["nodes"] if n["id"] == step_id), None) if plan else None
            if step_id and (
                not parent
                or parent["kind"] != "task"
                or plan["runtime"][step_id]["status"] == "completed"
                or any(
                    plan["runtime"][dep]["status"] != "completed" for dep in parent["depends_on"]
                )
            ):
                raise AgentError("step_not_ready", "研究步骤不存在、已完成或上游尚未完成。", 409)
            settings = JobSettings(
                host=args.host,
                workdir=directory,
                adopted=True,
                retrieve=args.retrieve,
                required_outputs=args.required_outputs,
                results=args.results,
            )
            from chemistry_agent.automation.monitor_schedules import (
                DEFAULT_INSTRUCTION,
                slurm_instruction,
            )

            instruction = args.instruction or DEFAULT_INSTRUCTION
            if not old and not args.instruction and plans.store:
                async with plans.store.lock:
                    instruction = await slurm_instruction(plans.store)
            record = old or {
                **observed,
                "id": identifier,
                "session_id": session,
                "host": args.host,
                "origin": "adopted",
                "completion_key": str(uuid5(NAMESPACE_URL, f"slurm:{args.host}:{identity}")),
                "settings": settings.model_dump(mode="json"),
                "created_at": utc_now(),
                "plan_id": plan_id,
                "node_id": node_id,
                "step_id": step_id,
                "instruction": instruction,
            }
            if record.get("terminal"):
                record.setdefault("completed_at", utc_now())
            node = PlanNode(
                id=node_id,
                title=args.title,
                kind="job",
                purpose=record["instruction"][:1000],
                job=record["settings"],
                parent_task=step_id,
                group_path=[parent["title"]] if parent else None,
            ).stored_record()
            # Validate display metadata before saving; full instructions remain in the receipt.
            # Persist identity first: crash recovery always recreates the same plan/node.
            plans.slurm.save(session, record)
            if plan is None:
                plan = {
                    "id": plan_id,
                    "revision": 0,
                    "title": args.title,
                    "goal": record["instruction"],
                    "research_systems": [],
                    "research_tasks": [],
                    "status": "running",
                    "max_parallel": None,
                    "repair_limit": 2,
                    "nodes": [],
                    "runtime": {},
                    "snapshots": {},
                    "history": [],
                    "created_at": utc_now(),
                    "auto_queue": auto_queue.get(),
                    "authorized_run": context.run_id,
                }
            plan["nodes"].append(node)
            if parent:
                target = plan["runtime"][step_id]
                target["status"] = "running"
                target.setdefault("jobs", []).append(identifier)
                target.pop("completed_at", None)
            plan["runtime"][node_id] = {
                "status": "running",
                "generic_job_id": identifier,
                "job": plans.generic_snapshot(record),
                "adopted": True,
            }
            plan.update(
                status="running",
                revision=plan["revision"] + 1,
                changed_nodes=[node_id],
                removed_nodes=[],
            )
            plan.pop("reported", None)
            plan.pop("completed_at", None)
            plan["history"].append(
                {
                    "revision": plan["revision"],
                    "changed_nodes": [node_id],
                    "removed_nodes": [],
                    "action": "adopt",
                    "at": utc_now(),
                }
            )
            plans.save(session, plan)
            await plans.platform.results.ensure_job(
                session,
                plan_id,
                node_id,
                identifier,
                settings.results,
                step_id,
            )
            return binding(record, plan_id, node_id, reused=bool(old))


def binding(record: dict, plan_id: str, node_id: str, *, reused: bool) -> dict:
    return {
        "job_id": record["id"],
        "scheduler_job_id": record["scheduler_job_id"],
        "plan_id": plan_id,
        "node_id": node_id,
        "step_id": record.get("step_id"),
        "remote_directory": record["remote_directory"],
        "identity": record.get("identity"),
        "reused": reused,
        "submitted": False,
        "monitoring": True,
        "results_registered": True,
        "terminal_observed": record.get("terminal", False),
    }
