"""Durable, user-approved DAGs composed by the conversation agent.

The web worker owns graph scheduling; AiiDA owns each remote calculation. Every
submission is checkpointed by preview ID before the idempotent AiiDA submit call.
"""

import asyncio
import contextlib
import hashlib
import io
import json
import logging
from collections import Counter
from datetime import UTC
from functools import cached_property
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ase.io import read

from chemistry_agent.automation.configuration import CalculationConfiguration
from chemistry_agent.automation.constraints import poscar_text, restore_constraints
from chemistry_agent.automation.events import (
    QUOTA_HOLD,
    quota_rejection,
    task_state,
    utc_now,
    waiting_for_submit_quota,
)
from chemistry_agent.automation.job_models import JobSettings
from chemistry_agent.automation.modeling import execute_model
from chemistry_agent.automation.models import StructureInput
from chemistry_agent.automation.plan_authority import auto_queue, auto_request, repair_target
from chemistry_agent.automation.plan_files import stage_dependencies
from chemistry_agent.automation.plan_groups import display_nodes
from chemistry_agent.automation.plan_models import (
    NodeRepair,
    PlanDraft,
    PlanExtension,
    PlanFigure,
    PlanNode,
    PlanProgram,
    PlanQuery,
    ProgramSource,
)
from chemistry_agent.automation.plan_repair import diagnose, eligible, replace_node
from chemistry_agent.automation.plan_stop import PlanStop, job_ids
from chemistry_agent.automation.reporting import job_evidence, summarize_result
from chemistry_agent.automation.slurm_jobs import SlurmJobs
from chemistry_agent.errors import AgentError
from chemistry_agent.memory.capture import safely_capture_plan
from chemistry_agent.tools import Tool, ToolContext

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "blocked"}


def generated_files(directory: Path, aliases: set[str]) -> list[str]:
    """Expose generated artifacts, excluding staged inputs and interpreter bookkeeping."""
    excluded = {"inputs.json", "file_inputs.json", "model.py", "sandbox.sb", "execution.log"}
    excluded.update(f"{alias}.{suffix}" for alias in aliases for suffix in ("vasp", "extxyz"))
    files = []
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            relative = path.relative_to(directory)
            if (
                relative.parts[0] in aliases
                or relative.parts[0] in {"_dependency_cache", "_job_inputs"}
                or path.name in {"archive", ".mplconfig", ".cache", ".matplotlib"}
                or str(relative) in excluded
                or path.is_symlink()
            ):
                continue
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files.append(str(relative))
    return sorted(files)


class CalculationPlans:
    def __init__(self, chat) -> None:
        self.chat = chat
        self.slurm = SlurmJobs(chat)
        from chemistry_agent.platform.adapters.chat import ChatAgentAdapter
        from chemistry_agent.platform.dispatch import SessionDispatcher

        self.dispatcher = SessionDispatcher(self)
        self.dispatcher.register("chat", ChatAgentAdapter(self))
        self.event_tasks = self.dispatcher.tasks
        self.locks: dict[str, asyncio.Lock] = {}
        self.task: asyncio.Task | None = None
        self.summary_task: asyncio.Task | None = None
        self.projected: set[tuple[str, str, int, bool]] = set()
        self.store = None
        self.agent = self.provider = None
        self.stop_requests: set[tuple[str, str]] = set()
        self.repair_tasks: dict[tuple[str, str], asyncio.Task] = {}

    @cached_property
    def platform(self):
        from chemistry_agent.platform.service import ComputationPlatform

        return ComputationPlatform(self)

    def lock(self, session: str, plan: str) -> asyncio.Lock:
        return self.locks.setdefault(f"{session}/{plan}", asyncio.Lock())

    def save(self, session: str, plan: dict) -> None:
        plan["updated_at"] = utc_now()
        self.chat.save(session, "plans", plan["id"], plan)

    @staticmethod
    def public(plan: dict) -> dict:
        # Execution snapshots are retained privately; effective inputs remain reviewable.
        return {key: value for key, value in plan.items() if key != "snapshots"} | {
            "nodes": display_nodes(plan)
        }

    def records(self, session: str) -> list[dict]:
        return [self.public(item) for item in self.chat.list_records(session, "plans")]

    async def draft(self, args: PlanDraft, context: ToolContext) -> dict:
        session, identifier = context.session_id, str(args.plan_id or uuid4())
        if auto_queue.get() and not args.plan_id:
            identifier = str(
                uuid5(NAMESPACE_URL, f"plan:{session}:{auto_request.get() or context.run_id}")
            )
        async with self.lock(session, identifier):
            if auto_queue.get() and not args.plan_id:
                path = self.chat.directory(session, "plans") / f"{identifier}.json"
                if path.is_file():
                    return self.public(self.chat.load(session, "plans", identifier))
            old = self.chat.load(session, "plans", identifier) if args.plan_id else None
            if old and (old["status"] != "draft" or old["revision"] != args.expected_revision):
                raise AgentError(
                    "plan_revision_conflict", "方案已确认或版本已变化，请读取当前方案。", 409
                )
            snapshots, identities = {}, {}
            queue = auto_queue.get()
            configuration = CalculationConfiguration(self.chat.service.settings.aiida_path)
            for node in args.nodes:
                if node.execution_host and node.execution_host not in self.slurm.hosts():
                    raise AgentError("job_host_unknown", "请先登记研究步骤使用的 SSH 服务器。", 422)
                for reference in node.inputs.values():
                    if reference.structure_id:
                        self.chat.load(session, "structures", str(reference.structure_id))
                if node.job:
                    if node.job.adopted:
                        raise AgentError(
                            "use_job_adopt", "已有作业请用 calculation_job_adopt 登记。", 422
                        )
                    if node.job.host not in self.slurm.hosts():
                        raise AgentError("job_host_unknown", "请先登记 SSH 服务器。", 422)
                    if queue and node.job.resources.queue != queue:
                        raise AgentError("wrong_queue", f"本轮只授权 {queue} 队列。", 422)
                if node.calculation:
                    settings = node.calculation
                    protocol = configuration.resolve(
                        settings.protocol_id, settings.incar, settings.kmesh
                    ).model_dump(mode="json")
                    protocol["incar"] = {k.upper(): v for k, v in protocol["incar"].items()}
                    protocol["incar"].update({k.upper(): v for k, v in settings.incar.items()})
                    if settings.kmesh:
                        protocol.update(kmesh=list(settings.kmesh), kpoint_spacing=None)
                    if settings.resources:
                        protocol["resources"].update(
                            settings.resources.model_dump(mode="json", exclude_unset=True)
                        )
                    source = configuration.get("sources", protocol["potential_source_id"])
                    if queue and protocol["resources"]["queue"] != queue:
                        raise AgentError("wrong_queue", f"本轮只授权 {queue} 队列。", 422)
                    key = (protocol["server_id"], protocol["code_id"])
                    if key not in identities:
                        identities[key] = await self.chat.service.call("plan_identity", protocol)
                    identity = identities[key]
                    snapshots[node.id] = {"protocol": protocol, "source": source, **identity}
            previous = {node["id"]: node for node in old["nodes"]} if old else {}
            nodes = [node.stored_record(previous.get(node.id)) for node in args.nodes]
            for node in nodes:
                if node["id"] in snapshots:
                    protocol = snapshots[node["id"]]["protocol"]
                    node["effective"] = {
                        key: protocol[key]
                        for key in (
                            "incar",
                            "kmesh",
                            "kpoint_spacing",
                            "resources",
                            "server_id",
                            "code_id",
                        )
                    }
            changed = [node["id"] for node in nodes if previous.get(node["id"]) != node]
            removed = sorted(previous.keys() - {node["id"] for node in nodes})
            revision = old["revision"] + 1 if old else 1
            plan = {
                "id": identifier,
                "revision": revision,
                "title": args.title,
                "goal": args.goal,
                "research_systems": args.research_systems
                or (old.get("research_systems", []) if old else []),
                "research_tasks": args.research_tasks
                or (old.get("research_tasks", []) if old else []),
                "status": "running" if queue else "draft",
                "max_parallel": None if queue else args.max_parallel,
                "repair_limit": args.repair_limit,
                **(
                    {
                        "auto_queue": queue,
                        "approved_at": utc_now(),
                        "approved_revision": revision,
                        "authorized_run": context.run_id,
                    }
                    if queue
                    else {}
                ),
                "nodes": nodes,
                "snapshots": snapshots,
                "changed_nodes": changed,
                "removed_nodes": removed,
                "created_at": old["created_at"] if old else utc_now(),
                "runtime": {node.id: {"status": "pending"} for node in args.nodes},
                "history": (old["history"] if old else [])
                + [
                    {
                        "revision": revision,
                        "changed_nodes": changed,
                        "removed_nodes": removed,
                        "at": utc_now(),
                    }
                ],
            }
            if old:
                # Immutable revision bodies permit later audit of conversation edits.
                self.chat.save(session, "plan_versions", str(uuid4()), old)
            self.save(session, plan)
            await safely_capture_plan(self.store, session, plan)
            return self.public(plan)

    async def extend(self, args: PlanExtension, context: ToolContext) -> dict:
        from chemistry_agent.automation.job_events import job_event_target

        session, identifier = context.session_id, str(args.plan_id)
        requested = args.parsed_nodes()
        for node in requested:
            node.depends_on = sorted(
                set(node.depends_on)
                | {ref.node for ref in node.inputs.values() if ref.node}
                | {ref.node for ref in node.files if ref.node}
            )
        if job_event_target.get() != (session, identifier):
            raise AgentError("extension_scope", "请在此方案的任务结束回合追加步骤。", 403)
        async with self.lock(session, identifier):
            plan = self.chat.load(session, "plans", identifier)
            if not plan.get("auto_queue") or plan["status"] in {"paused", "stopping", "stopped"}:
                raise AgentError(
                    "extension_not_authorized", "未授权自动后续计算，请提出草稿。", 403
                )
            existing = {node["id"] for node in plan["nodes"]}
            if existing & {node.id for node in requested}:
                # Stable node IDs make repeated tool calls idempotent.
                proposed = {node.id: node.stored_record() for node in requested}
                old = {
                    node["id"]: PlanNode.model_validate(
                        {k: v for k, v in node.items() if k in PlanNode.model_fields}
                    ).stored_record()
                    for node in plan["nodes"]
                    if node["id"] in proposed
                }
                if old == proposed:
                    return {
                        "id": identifier,
                        "revision": plan["revision"],
                        "status": plan["status"],
                        "added_nodes": [node.id for node in requested],
                    }
                raise AgentError("node_id_conflict", "已有节点保持不变，请使用新节点 ID。", 409)
            previous = [
                PlanNode.model_validate(
                    {k: v for k, v in node.items() if k in PlanNode.model_fields}
                )
                for node in plan["nodes"]
            ]
            validated = PlanDraft(
                title=plan["title"], goal=plan["goal"], nodes=previous + requested
            )
            additions = validated.nodes[len(previous) :]
            snapshots = {}
            configuration = CalculationConfiguration(self.chat.service.settings.aiida_path)
            for node in additions:
                if node.execution_host and node.execution_host not in self.slurm.hosts():
                    raise AgentError("job_host_unknown", "未登记此服务器。", 422)
                if node.job and node.job.adopted:
                    raise AgentError(
                        "use_job_adopt", "已有作业请用 calculation_job_adopt 登记。", 422
                    )
                if node.job and node.job.resources.queue != plan["auto_queue"]:
                    raise AgentError("wrong_queue", "后续任务沿用已授权队列。", 422)
                if node.job and node.job.host not in self.slurm.hosts():
                    raise AgentError("job_host_unknown", "未登记此服务器。", 422)
                if node.calculation:
                    c = node.calculation
                    protocol = configuration.resolve(c.protocol_id, c.incar, c.kmesh).model_dump(
                        mode="json"
                    )
                    if c.resources:
                        protocol["resources"].update(
                            c.resources.model_dump(mode="json", exclude_unset=True)
                        )
                    if protocol["resources"]["queue"] != plan["auto_queue"]:
                        raise AgentError("wrong_queue", "后续任务沿用已授权队列。", 422)
                    snapshots[node.id] = {
                        "protocol": protocol,
                        "source": configuration.get("sources", protocol["potential_source_id"]),
                        **await self.chat.service.call("plan_identity", protocol),
                    }
            self.chat.save(session, "plan_versions", str(uuid4()), plan)
            plan["snapshots"].update(snapshots)
            plan["nodes"].extend(node.stored_record() for node in additions)
            plan["runtime"].update({node.id: {"status": "pending"} for node in additions})
            plan["revision"] += 1
            plan["changed_nodes"] = [node.id for node in additions]
            plan["history"].append(
                {
                    "revision": plan["revision"],
                    "changed_nodes": plan["changed_nodes"],
                    "removed_nodes": [],
                    "reason": args.reason,
                    "at": utc_now(),
                }
            )
            plan.update(status="running", reported=False)
            plan.pop("completed_at", None)
            self.save(session, plan)
            return {
                "id": identifier,
                "revision": plan["revision"],
                "status": plan["status"],
                "added_nodes": [node.id for node in requested],
            }

    async def repair_node(self, args: NodeRepair, context: ToolContext) -> dict:
        return await replace_node(self, args, context)

    async def compile_program(self, args: PlanProgram, context: ToolContext) -> dict:
        """Expand model-written loops locally so repeated branches do not consume context."""
        root = self.chat.directory(context.session_id, "plan_programs")
        directory = root / str(uuid4())
        directory.mkdir(mode=0o700)
        if args.previous_id:
            source = self.program_text(root, str(args.previous_id))
            (directory / "previous.py").write_text(source)
        (directory / "plan_writer.py").write_text(
            Path(__file__).with_name("plan_writer.py").read_text()
        )
        try:
            content = await execute_model(directory, args.code, output_format="json")
            plan = await self.draft(PlanDraft.model_validate_json(content), context)
        except AgentError as exc:
            raise AgentError(
                exc.code, f"program_id={directory.name}\n{exc.message}", exc.status_code
            ) from exc
        except ValueError as exc:
            from pydantic import ValidationError

            detail = (
                [
                    {"field": e["loc"], "reason": e["msg"]}
                    for e in exc.errors(include_input=False)[:8]
                ]
                if isinstance(exc, ValidationError)
                else str(exc)[:1000]
            )
            raise AgentError(
                "plan_program_invalid", f"program_id={directory.name}\n{detail}", 422
            ) from exc
        return {k: plan[k] for k in ("id", "title", "revision", "status", "max_parallel")} | {
            "nodes": len(plan["nodes"]),
            "calculations": sum(n["kind"] == "vasp" for n in plan["nodes"]),
        }

    @staticmethod
    def program_text(root: Path, identifier: str) -> str:
        path = root / str(UUID(identifier)) / "model.py"
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise AgentError("program_missing", "此对话中没有可读取的方案程序。", 404)
        return path.read_text()

    async def program_source(self, args: ProgramSource, context: ToolContext) -> dict:
        root = self.chat.directory(context.session_id, "plan_programs")
        if args.program_id is None:
            return {
                "program_ids": [
                    p.name
                    for p in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                    if p.is_dir()
                ]
            }
        lines = self.program_text(root, str(args.program_id)).splitlines()
        first = args.first_line - 1
        return {
            "program_id": str(args.program_id),
            "total_lines": len(lines),
            "first_line": first + 1,
            "source": "\n".join(lines[first : first + args.lines])[:16000],
        }

    async def add_figure(self, args: PlanFigure, context: ToolContext) -> dict:
        """Attach local post-processing to an existing result, including a future result."""
        session, identifier = context.session_id, str(args.plan_id)
        key = "plot_" + uuid5(NAMESPACE_URL, args.model_dump_json()).hex
        node = PlanNode(
            id=key,
            kind="analysis",
            title=args.title,
            code=args.code,
            inputs={"data": {"node": args.source_node, "output": "result"}},
            depends_on=[args.source_node],
        )
        async with self.lock(session, identifier):
            plan = self.chat.load(session, "plans", identifier)
            if args.source_node not in plan["runtime"]:
                raise AgentError("missing_source", "找不到绘图所需的上游步骤。", 422)
            if key not in plan["runtime"]:
                self.chat.save(session, "plan_versions", str(uuid4()), plan)
                plan.setdefault("figure_nodes", []).append(key)
                plan["nodes"].append(node.stored_record())
                plan["runtime"][key] = {"status": "pending"}
                plan["changed_nodes"], plan["removed_nodes"] = [key], []
                plan["revision"] += 1
                plan["history"].append(
                    {
                        "revision": plan["revision"],
                        "changed_nodes": [key],
                        "removed_nodes": [],
                        "at": utc_now(),
                    }
                )
                if plan["status"] in {"completed", "failed"}:
                    plan["status"] = "running"
                    plan.pop("completed_at", None)
                plan.pop("reported", None)
                self.save(session, plan)
            return {"plan_id": identifier, "node_id": key, "status": plan["runtime"][key]["status"]}

    async def stop(self, args: PlanStop, context: ToolContext) -> dict:
        if not auto_request.get() or repair_target.get():
            raise AgentError("stop_not_authorized", "仅可在用户对话回合中按要求停止本方案。", 403)
        session, identifier = context.session_id, str(args.plan_id)
        self.chat.load(session, "plans", identifier)
        self.stop_requests.add((session, identifier))
        event_task = self.event_tasks.get((session, identifier))
        if event_task:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
        async with self.lock(session, identifier):
            self.stop_requests.discard((session, identifier))
            plan = self.chat.load(session, "plans", identifier)
            if self.store:
                async with self.store.transaction():
                    await self.store.db.execute(
                        "UPDATE agent_waits SET status='cancelled' "
                        "WHERE session_id=? AND plan_id=?",
                        (session, identifier),
                    )
                    await self.store.db.execute(
                        "UPDATE agent_events SET status='cancelled' "
                        "WHERE session_id=? AND plan_id=? "
                        "AND status NOT IN ('completed','superseded')",
                        (session, identifier),
                    )
            plan["status"] = "stopping"
            self.save(session, plan)
            ids = job_ids(plan)
            result = (
                await self.chat.service.call("stop_plan_jobs", {"job_ids": ids})
                if ids
                else {"verified_stopped": True, "active_aiida": [], "active_scheduler": []}
            )
            for state in plan["runtime"].values():
                for attempt in [
                    state,
                    *(a["state"] for a in state.get("attempts", []) if "state" in a),
                ]:
                    if not attempt.get("generic_job_id"):
                        continue
                    receipt = (
                        self.chat.directory(session, "slurm_jobs")
                        / f"{attempt['generic_job_id']}.json"
                    )
                    if not receipt.exists():
                        continue
                    record = self.slurm.load(session, attempt["generic_job_id"])
                    if record.get("scheduler_job_id") and not record.get("terminal"):
                        await self.slurm.remote(record, "cancel")
                        record = await self.slurm.inspect(session, record["id"])
                        if not record.get("terminal"):
                            result["verified_stopped"] = False
                            result["active_scheduler"].append(record["scheduler_job_id"])
                    elif record["status"] == "uncertain":
                        result["verified_stopped"] = False

            if result["verified_stopped"]:
                plan["status"] = "stopped"
                for state in plan["runtime"].values():
                    if state["status"] not in TERMINAL:
                        state["status"] = "stopped"
            plan["stop_result"] = result
            self.save(session, plan)
            return {"status": plan["status"], **result}

    async def query(self, args: PlanQuery, context: ToolContext) -> dict:
        def summary(plan: dict) -> dict:
            return {
                key: plan[key] for key in ("id", "revision", "title", "status", "updated_at")
            } | {
                "node_count": len(plan["nodes"]),
                "states": dict(Counter(state["status"] for state in plan["runtime"].values())),
                "failed_nodes": [k for k, v in plan["runtime"].items() if v["status"] == "failed"],
            }

        if args.plan_id:
            plan = self.chat.load(context.session_id, "plans", str(args.plan_id))
            if args.node_id:
                node = next((n for n in display_nodes(plan) if n["id"] == args.node_id), None)
                if not node:
                    raise AgentError("missing_node", "此方案中没有指定节点。", 404)
                return {"node": node, "state": plan["runtime"][args.node_id]}
            if args.full or len(plan["nodes"]) <= 20:
                return self.public(plan)
            return summary(plan) | {"node_ids": [n["id"] for n in plan["nodes"]]}
        return {"plans": [summary(plan) for plan in self.records(context.session_id)]}

    async def confirm(self, session: str, identifier: str, revision: int) -> dict:
        async with self.lock(session, identifier):
            plan = self.chat.load(session, "plans", identifier)
            if plan["revision"] != revision:
                raise AgentError("plan_revision_conflict", "方案已更新，请查看新版后确认。", 409)
            if plan["status"] == "draft":
                plan.update(status="running", approved_at=utc_now(), approved_revision=revision)
                self.save(session, plan)
            return self.public(plan)

    async def control(self, session: str, identifier: str, action: str) -> dict:
        async with self.lock(session, identifier):
            plan = self.chat.load(session, "plans", identifier)
            if action == "rediagnose":
                if not plan.get("auto_queue") or plan["status"] not in {"failed", "running"}:
                    raise AgentError("plan_state_conflict", "仅可重新诊断自动方案的失败节点。", 409)
                if (session, identifier) in self.repair_tasks or (
                    session,
                    identifier,
                ) in self.event_tasks:
                    raise AgentError("session_busy", "已有故障诊断正在运行。", 409)
                for event in plan.get("job_events", {}).values():
                    if (
                        event["status"] in {"uncertain", "completed"}
                        and event["outcome"] == "failed"
                    ):
                        event["status"] = "pending"
                for state in plan["runtime"].values():
                    if state["status"] == "failed":
                        state.pop("diagnosis_started", None)
                self.save(session, plan)
                return self.public(plan)
            expected = "running" if action == "pause" else "paused"
            if plan["status"] != expected:
                raise AgentError("plan_state_conflict", "当前状态不能执行此操作。", 409)
            plan["status"] = "paused" if action == "pause" else "running"
            self.save(session, plan)
            return self.public(plan)

    def resolved_inputs(self, session: str, plan: dict, node: dict) -> dict:
        result = {}
        for alias, ref in node["inputs"].items():
            if ref["structure_id"]:
                result[alias] = {"structure_id": ref["structure_id"]}
            else:
                upstream = plan["runtime"][ref["node"]]
                if upstream["status"] != "completed":
                    raise AgentError("dependency_pending", "上游步骤尚未完成。", 409)
                output = upstream.get("outputs", {})
                key = "structure_id" if ref["output"] == "structure" else "result"
                if key not in output:
                    raise AgentError("plan_output_missing", f"上游 {ref['node']} 缺少 {key}。", 422)
                result[alias] = {key: output[key]}
        return result

    async def run_code(self, session: str, plan: dict, node: dict, state: dict) -> dict:
        from chemistry_agent.automation.chat import ImportInput

        directory = self.chat.directory(session, "plan_runs") / plan["id"] / node["id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = directory / ("structure.vasp" if node["kind"] == "model" else "result.json")
        output.unlink(missing_ok=True)
        inputs = self.resolved_inputs(session, plan, node)
        await stage_dependencies(self, session, plan, node, directory)
        for alias, value in inputs.items():
            if "structure_id" in value:
                data = self.chat.load(session, "structures", value["structure_id"])
                atoms = read(io.StringIO(data["extxyz"]), format="extxyz")
                restore_constraints(atoms)
                (directory / f"{alias}.vasp").write_text(poscar_text(atoms))
                (directory / f"{alias}.extxyz").write_text(data["extxyz"])
                value["file"] = f"{alias}.vasp"
            elif node.get("result_files") and value.get("result", {}).get("job_id"):
                job_id = value["result"]["job_id"]
                await self.chat.require_session_job(session, job_id)
                staged = await self.chat.service.call(
                    "stage_files",
                    {
                        "job_id": job_id,
                        "files": node["result_files"],
                        "source": node.get("file_source", "retrieved"),
                        "directory": str(directory / alias),
                    },
                )
                value["files"] = {
                    name: str(Path(path).relative_to(directory))
                    for name, path in staged["files"].items()
                }
        (directory / "inputs.json").write_text(json.dumps(inputs, ensure_ascii=False))
        # A model may call its input alias "structure" and read/write structure.vasp.
        # Preserve that input, but don't mistake an untouched input for a generated output.
        input_version = output.stat().st_mtime_ns if output.exists() else None
        async with self.chat.model_lock:
            content = await execute_model(
                directory, node["code"], output_format="vasp" if node["kind"] == "model" else "json"
            )
        if input_version is not None and output.stat().st_mtime_ns == input_version:
            raise AgentError("model_output_missing", "建模代码未写出新结构。", 422)
        if node["kind"] == "model":
            imported = await self.chat.import_structure(
                ImportInput(
                    name=node["title"], structure=StructureInput(format="vasp", content=content)
                ),
                ToolContext(session, plan["id"]),
            )
            return {
                "structure_id": imported["id"],
                "result": self.chat.structure_summary(imported),
                "files": generated_files(directory, set(inputs)),
            }
        result = json.loads(
            content,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"分析结果不能含 {value}")
            ),
        )
        if not isinstance(result, dict):
            raise AgentError("invalid_analysis", "分析代码须写出 JSON 对象 result.json。", 422)
        if result.get("passed") is False:
            raise AgentError(
                "analysis_gate_failed",
                str(result.get("reason") or result.get("error") or "分析检查未通过。"),
                422,
            )
        files = generated_files(directory, set(inputs))
        if node["id"] in plan.get("figure_nodes", []) and not any(
            name.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".webp")) for name in files
        ):
            raise AgentError("missing_figure", "绘图代码未生成图像，请使用 savefig 保存图片。", 422)
        return {"result": result, "files": files}

    async def start_job(self, session: str, plan: dict, node: dict, state: dict) -> None:
        if node["job"].get("adopted"):
            raise AgentError("adopted_not_submittable", "接管节点只监控已有作业，不重新提交。", 409)
        identifier = state.setdefault("generic_job_id", str(uuid4()))
        self.save(session, plan)
        directory = self.chat.directory(session, "plan_runs") / plan["id"] / node["id"]
        # A fresh per-attempt input tree prevents an old restart/output becoming a new input.
        inputs_dir = directory / "_job_inputs" / identifier
        receipt = self.chat.directory(session, "slurm_jobs") / f"{identifier}.json"
        settings = JobSettings.model_validate(node["job"])
        if not receipt.exists():
            inputs_dir.mkdir(parents=True, exist_ok=True)
            await stage_dependencies(
                self,
                session,
                plan,
                node,
                inputs_dir,
                remote_destination={
                    "host": settings.host,
                    "remote_directory": settings.workdir.rstrip("/") + "/" + identifier,
                },
            )
            resolved = self.resolved_inputs(session, plan, node)
            for alias, value in resolved.items():
                if "structure_id" in value:
                    structure = self.chat.load(session, "structures", value["structure_id"])
                    (inputs_dir / f"{alias}.extxyz").write_text(structure["extxyz"])
                    value["file"] = f"{alias}.extxyz"
            for name, text in settings.files.items():
                path = inputs_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            (inputs_dir / "inputs.json").write_text(json.dumps(resolved, ensure_ascii=False))
        record = await self.slurm.submit(session, identifier, settings, inputs_dir)
        if node.get("parent_task"):
            target = plan["runtime"][node["parent_task"]]
            if identifier not in target.setdefault("jobs", []):
                target["jobs"].append(identifier)
            target["status"] = "running"
            record.update(step_id=node["parent_task"], plan_id=plan["id"], node_id=node["id"])
            self.slurm.save(session, record)
        state.update(status="running", job=self.generic_snapshot(record))
        if record["status"] == "rejected":
            raise AgentError("job_rejected", record.get("error", "Slurm 拒绝提交。"), 422)

    @staticmethod
    def generic_snapshot(record: dict) -> dict:
        status = record.get("scheduler_state", "").lower()
        return {
            **record,
            "backend": "slurm",
            "process_state": "finished" if record.get("terminal") else "waiting",
            "scheduler_state": status,
            "submission_state": "rejected_by_scheduler"
            if record["status"] == "quota_wait"
            else "accepted_by_scheduler"
            if record.get("scheduler_job_id")
            else "not_submitted",
            "paused": record["status"] == "quota_wait",
            "process_status": record.get("error"),
            "pk": None,
        }

    async def inspect_job(self, session: str, plan: dict, node: dict, state: dict) -> None:
        from datetime import datetime

        checked = state.get("polled_at")
        if checked and (datetime.now(UTC) - datetime.fromisoformat(checked)).total_seconds() < 30:
            return
        state["polled_at"] = utc_now()
        record = await self.slurm.inspect(session, state["generic_job_id"], release_waiter=True)
        state["job"] = self.generic_snapshot(record)
        if record["status"] == "rejected":
            raise AgentError("job_rejected", record.get("error", "Slurm 拒绝提交。"), 422)
        if not record.get("terminal"):
            return
        directory = self.chat.directory(session, "plan_runs") / plan["id"] / node["id"]
        # Retrieval failures keep the node tracked, and never resubmit the calculation.
        files = await self.slurm.retrieve(record, directory / "attempts" / record["id"])
        files["files"] = [f"attempts/{record['id']}/{name}" for name in files["files"]]
        state["outputs"] = {
            **files,
            "result": {
                "job_id": record["id"],
                "backend": "slurm",
                "scheduler_job_id": record.get("scheduler_job_id"),
                "scheduler_state": record.get("scheduler_state"),
                "exit_code": record.get("exit_code"),
                "script_exit_code": record.get("script_exit_code"),
            },
        }
        state["completed_at"] = record["completed_at"]
        if not record.get("finished_ok") or files["missing_outputs"]:
            raise AgentError(
                "job_failed",
                "作业结束："
                + str(record.get("scheduler_state"))
                + "；缺失输出："
                + ", ".join(files["missing_outputs"]),
                422,
            )
        state.update(status="completed")

    async def start_vasp(self, session: str, plan: dict, node: dict, state: dict) -> None:
        from chemistry_agent.automation.chat import ChatPreviewInput

        if not state.get("preview_id"):
            snapshot = plan["snapshots"][node["id"]]
            current = await self.chat.service.call("plan_identity", snapshot["protocol"])
            if current["identity"] != snapshot["identity"]:
                raise AgentError(
                    "plan_environment_changed", "服务器或 VASP 环境已变化，请重新设计并确认。", 409
                )
            inputs = self.resolved_inputs(session, plan, node)
            structure_id = inputs["structure"]["structure_id"]
            structure = self.chat.load(session, "structures", structure_id)
            resolved = await self.chat.service.call(
                "resolve_calculation",
                {
                    "protocol_id": snapshot["protocol"]["id"],
                    "protocol_snapshot": snapshot["protocol"],
                    "source_snapshot": snapshot["source"],
                    "structure": structure["structure"],
                },
            )
            request = {k: v for k, v in resolved["request"].items() if k != "structure"}
            preview = await self.chat.preview(
                ChatPreviewInput(structure_id=structure_id, **request),
                ToolContext(session, plan["id"]),
            )
            state.update(
                preview_id=preview["preview_id"],
                input_structure_id=structure_id,
                input_structure_fingerprint=hashlib.sha256(
                    json.dumps(structure["structure"], sort_keys=True).encode()
                ).hexdigest(),
            )
            artifact = self.chat.load(session, "previews", preview["preview_id"])
            artifact.update(
                plan_id=plan["id"],
                plan_node=node["id"],
                plan_revision=plan["revision"],
                plan_auto=bool(plan.get("auto_queue")),
            )
            self.chat.save(session, "previews", preview["preview_id"], artifact)
            # Persist BEFORE submission. Recovery always uses this same preview.
            self.save(session, plan)
        submission = await self.chat.submit_reviewed(session, state["preview_id"])
        state.update(status="running", job_id=submission["job"]["id"], job=submission["job"])

    async def inspect_vasp(self, session: str, plan: dict, node: dict, state: dict) -> None:
        from chemistry_agent.automation.chat import JobInput

        job = await self.chat.service.call("job_details", {"job_id": state["job_id"]})
        state["job"] = job
        if job.get("scheduler_job_id"):
            state.pop("quota_wait_since", None)
            state.pop("quota_move", None)
        move = state.get("quota_move")
        if move and job["process_state"] == "killed" and not job.get("scheduler_job_id"):
            protocol = plan["snapshots"][node["id"]]["protocol"]
            resources = protocol["resources"] | {"queue": move["target"]}
            protocol["resources"] = resources
            node["effective"]["resources"] = node["calculation"]["resources"] = resources
            history = state.get("quota_history", []) + [
                dict(move, job_id=state["job_id"], pk=job["pk"], preview_id=state["preview_id"])
            ]
            attempts = state.get("attempts", [])
            state.clear()
            state.update(status="pending", attempts=attempts, quota_history=history)
            self.save(session, plan)
            return
        if job["process_state"] not in {"finished", "excepted", "killed"}:
            return
        if not job.get("finished_ok"):
            raise AgentError(
                "plan_calculation_failed", job.get("exit_message") or "计算异常结束。", 422
            )
        run_status = job.get("results", {}).get("run_status", {})
        settings = node["calculation"]
        for flag in ("electronic", "ionic"):
            if settings[f"require_{flag}"] and run_status.get(f"{flag}_converged") is not True:
                raise AgentError("plan_not_converged", f"{flag} 收敛未通过或缺少证据。", 422)
        outputs = {"result": {"job_id": job["id"], "pk": job["pk"], **job.get("results", {})}}
        if job.get("output_structure"):
            imported = await self.chat.import_job_structure(
                JobInput(job_id=job["id"]), ToolContext(session, job["id"])
            )
            outputs["structure_id"] = imported["id"]
        else:
            # Static jobs may not expose a new structure: their input geometry is the output.
            incar = {k.upper(): v for k, v in node["effective"]["incar"].items()}
            if not int(incar.get("NSW", 0)):
                outputs["structure_id"] = state["input_structure_id"]
        state.update(
            status="completed", outputs=outputs, completed_at=utc_now(), summary_requested=True
        )

    async def resume_quota_waiter(self, session: str, plan: dict, node: dict, state: dict) -> None:
        job = state.get("job", {})
        if plan["status"] != "running" or state.get("status") != "running":
            return
        if (
            job.get("paused")
            and job.get("scheduler_job_id")
            and job.get("process_status") == QUOTA_HOLD
        ):
            await self.chat.service.call("hold_quota_job", {"job_id": state["job_id"]})
            return
        rejection = quota_rejection(job)
        if not rejection:
            return
        protocol = plan["snapshots"][node["id"]]["protocol"]
        policy = protocol.get("quota_fallback")
        if "quota_fallback" not in protocol:
            # Plans reviewed before this feature may use the owner's registered policy.
            config = CalculationConfiguration(self.chat.service.settings.aiida_path)
            registered = config.get("protocols", protocol["id"])
            if registered["server_id"] == protocol["server_id"]:
                policy = registered.get("quota_fallback")
        if not (policy or plan.get("auto_queue")):
            return
        if not waiting_for_submit_quota(job):
            if (
                not job.get("paused")
                and job.get("process_status") == "Waiting for transport task: submit"
                and rejection.get("time")
                and rejection["time"] != state.get("quota_released_error")
            ):
                state.setdefault("quota_wait_since", utc_now())
                await self.chat.service.call(
                    "hold_quota_job", {"job_id": state["job_id"], "time": rejection["time"]}
                )
            return
        resources = protocol["resources"]
        queue = resources["queue"]
        target = policy["target"] if policy and queue == policy["source"] else queue
        since = state.setdefault("quota_wait_since", utc_now())
        move = state.get("quota_move")
        if not move:
            monitor = getattr(self.chat, "monitor", None)
            if not monitor:
                return
            release = monitor.events.claim_quota_release(
                protocol["server_id"],
                resources.get("account") or "",
                target,
                state["job_id"],
                since,
                rejection.get("time") or since,
                source_queue=queue,
            )
            if not release:
                return
            state["quota_released_error"] = rejection.get("time")
            state["quota_release"] = release
            state["quota_wait_since"] = utc_now()
            if target != queue:
                state["quota_move"] = {"source": queue, "target": target, "released_by": release}
            self.save(session, plan)
        if state.get("quota_move"):
            target = state["quota_move"]["target"]
            await self.chat.service.call(
                "retire_quota_job",
                {
                    "job_id": state["job_id"],
                    "source": queue,
                    "resources": resources | {"queue": target},
                },
            )
        else:
            await self.chat.service.call("resume_quota_job", {"job_id": state["job_id"]})

    @staticmethod
    def step_status(state: dict) -> str:
        if state["status"] == "running" and state.get("job"):
            return task_state(state["job"])[0]
        return state["status"]

    def record_steps(self, session: str, plan: dict, nodes: list[dict]) -> None:
        """Project saved checkpoints, including pre-restart attempts, without waking a model."""
        with self.chat.progress.connect() as db:
            for node in nodes:
                state = plan["runtime"][node["id"]]
                attempts = state.get("attempts", [])
                for attempt, saved in enumerate(
                    [*(a["state"] for a in attempts if "state" in a), state]
                ):
                    job = saved.get("job") or {}
                    status = self.step_status(saved)
                    if status == "pending" and plan["status"] != "stopped":
                        continue
                    detail = {
                        "attempt": attempt,
                        "node_kind": node["kind"],
                        "completed_at": saved.get("completed_at"),
                        "job_id": job.get("id"),
                        "pk": job.get("pk"),
                        "scheduler_job_id": job.get("scheduler_job_id"),
                        "error": saved.get("error"),
                        "structure_id": (saved.get("outputs") or {}).get("structure_id"),
                    }
                    key = f"step:{node['id']}:{attempt}:{status}"
                    self.chat.progress.insert(
                        db,
                        session,
                        plan["id"],
                        node["id"],
                        "step",
                        status,
                        node["title"],
                        detail,
                        key,
                    )
                    if (
                        saved.get("summary_requested")
                        and saved["status"] == "completed"
                        and node["kind"] == "vasp"
                        and plan["status"] not in {"stopping", "stopped"}
                    ):
                        evidence = job_evidence(job) | {
                            "step": node["title"],
                            "purpose": node.get("purpose"),
                            "scheduler_job_id": job.get("scheduler_job_id"),
                        }
                        self.chat.progress.queue_summary(db, session, plan["id"], key, evidence)

    async def tick_plan(self, session: str, identifier: str) -> None:
        async with self.lock(session, identifier):
            plan = self.chat.load(session, "plans", identifier)
            stopped = plan["status"] in {"stopping", "stopped"}
            projection_key = (session, identifier, plan["revision"], plan["status"] == "stopped")
            if projection_key not in self.projected:
                self.record_steps(session, plan, plan["nodes"])
                self.projected.add(projection_key)
            if stopped:
                await safely_capture_plan(self.store, session, plan)
                return
            if plan["status"] in {"running", "paused"}:
                for node in plan["nodes"]:
                    if (session, identifier) in self.stop_requests:
                        return
                    state = plan["runtime"][node["id"]]
                    before = (state["status"], self.step_status(state))
                    try:
                        if state.get("generic_job_id") and state["status"] == "running":
                            await self.inspect_job(session, plan, node, state)
                        if state.get("job_id") and state["status"] == "running":
                            await self.inspect_vasp(session, plan, node, state)
                            await self.resume_quota_waiter(session, plan, node, state)
                        if state["status"] in TERMINAL or plan["status"] == "paused":
                            continue
                        dependencies = [
                            plan["runtime"][dep]["status"] for dep in node["depends_on"]
                        ]
                        if any(status in {"failed", "blocked"} for status in dependencies):
                            state.update(status="blocked", error="上游步骤失败，未执行。")
                            continue
                        if not all(status == "completed" for status in dependencies):
                            continue
                        if node.get("executor") == "agent" or node["kind"] == "task":
                            continue  # Readiness becomes an event; only the agent owns execution.
                        if state.get("job_id") or (
                            state.get("generic_job_id") and state["status"] == "running"
                        ):
                            continue
                        active = sum(
                            bool(s.get("job_id") or s.get("generic_job_id"))
                            and s["status"] == "running"
                            for s in plan["runtime"].values()
                        )
                        if (
                            node["kind"] in {"vasp", "job"}
                            and plan.get("max_parallel")
                            and active >= plan["max_parallel"]
                        ):
                            continue
                        state.update(
                            status="preparing", started_at=state.get("started_at", utc_now())
                        )
                        self.save(session, plan)
                        if node["kind"] == "job":
                            await self.start_job(session, plan, node, state)
                        elif node["kind"] == "vasp":
                            await self.start_vasp(session, plan, node, state)
                        else:
                            outputs = await self.run_code(session, plan, node, state)
                            state.update(
                                status="completed", outputs=outputs, completed_at=utc_now()
                            )
                        state.pop("sync_error", None)
                    except AgentError as exc:
                        # Query/network outages are not calculation failures. Keep tracking the job.
                        if (
                            state.get("job_id") or state.get("generic_job_id")
                        ) and exc.status_code >= 500:
                            state["sync_error"] = exc.message
                        else:
                            state.update(status="failed", error=exc.message, error_code=exc.code)
                    except (ValueError, OSError) as exc:
                        state.update(status="failed", error=str(exc))
                    finally:
                        if (state["status"], self.step_status(state)) != before:
                            self.save(session, plan)
                            self.record_steps(session, plan, [node])
                    # The graph-level checkpoint below also retains unchanged-phase job snapshots.
                states = [
                    plan["runtime"][n["id"]]["status"]
                    for n in plan["nodes"]
                    if not n.get("parent_task")
                ]
                if all(status in TERMINAL for status in states):
                    plan["status"] = (
                        "completed" if all(s == "completed" for s in states) else "failed"
                    )
                    plan["completed_at"] = utc_now()
                self.save(session, plan)
            await safely_capture_plan(self.store, session, plan)
            if (
                plan["status"] in {"completed", "failed"}
                and not plan.get("reported")
                and not (plan.get("auto_queue") and self.agent and eligible(plan))
                and (session, identifier) not in self.repair_tasks
                and self.store
            ):
                labels = {"completed": "已完成", "failed": "失败", "blocked": "上游失败，未执行"}
                rows = []
                for node in plan["nodes"]:
                    if (session, identifier) in self.stop_requests:
                        return
                    state = plan["runtime"][node["id"]]
                    reason = f" · {state['error']}" if state.get("error") else ""
                    rows.append(f"- {node['title']}：{labels[state['status']]}{reason}")
                summaries = [
                    json.dumps(
                        plan["runtime"][node["id"]].get("outputs", {}).get("result"),
                        ensure_ascii=False,
                    )[:4000]
                    for node in plan["nodes"]
                    if node["kind"] == "analysis"
                    and plan["runtime"][node["id"]]["status"] == "completed"
                ]
                title = "完成" if plan["status"] == "completed" else "有步骤失败"
                answer = f"方案「{plan['title']}」{title}。\n\n"
                answer += "\n".join(rows) + (
                    "\n\n分析结果：\n" + "\n".join(summaries) if summaries else ""
                )
                if await self.store.append_task_report(
                    session, f"plan:{identifier}:{plan['revision']}", answer
                ):
                    with self.chat.progress.connect() as db:
                        self.chat.progress.insert(
                            db,
                            session,
                            identifier,
                            None,
                            "plan",
                            plan["status"],
                            f"方案「{plan['title']}」{title}",
                            {
                                "revision": plan["revision"],
                                "chat_event": f"plan:{identifier}:{plan['revision']}",
                            },
                            f"final:{plan['revision']}",
                        )
                    plan["reported"] = True
                    self.save(session, plan)

            from chemistry_agent.automation.job_events import record_events
            from chemistry_agent.automation.wait_ledger import WaitLedger

            record_events(plan)
            self.save(session, plan)
            key = (session, identifier)
            if self.store:
                ledger = WaitLedger(self.store)
                await ledger.probe_files(self, session, plan)
                await ledger.observe(session, plan)
            await self.dispatcher.offer(session, plan)
            if (
                self.agent
                and plan.get("auto_queue")
                and plan["status"] != "paused"
                and eligible(plan)
                and key not in self.repair_tasks
            ):
                task = asyncio.create_task(diagnose(self, session, identifier))
                self.repair_tasks[key] = task
                task.add_done_callback(lambda _: self.repair_tasks.pop(key, None))

    async def watch(self) -> None:
        while True:
            for path in self.chat.root.glob("*/plans/*.json"):
                try:
                    await self.tick_plan(str(UUID(path.parent.parent.name)), str(UUID(path.stem)))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Plan scheduling delayed")
            await asyncio.sleep(5)

    async def interpret_next_step(self) -> bool:
        progress = self.chat.progress
        item = progress.claim_summary()
        if item is None:
            return False
        plan = self.chat.load(item["session_id"], "plans", item["plan_id"])
        if plan["status"] in {"stopped", "stopping"}:
            progress.finish_summary(item, "skipped", "方案已停止，跳过待处理解读。")
            return True
        status, answer = await summarize_result(
            self.provider, self.store, item["session_id"], item["evidence"]
        )
        progress.finish_summary(item, status, answer)
        return True

    async def watch_summaries(self) -> None:
        while True:
            try:
                if await self.interpret_next_step():
                    continue
            except Exception:
                logger.exception("Step interpretation delayed")
            await asyncio.sleep(5)

    def start(self, store, agent=None, provider=None) -> None:
        self.store, self.agent, self.provider = store, agent, provider
        self.task = asyncio.create_task(self.watch())
        self.dispatcher.start()
        if provider is not None and store is not None:
            self.summary_task = asyncio.create_task(self.watch_summaries())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.summary_task:
            self.summary_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.summary_task
        await self.dispatcher.close()
        for task in self.repair_tasks.values():
            task.cancel()
        await asyncio.gather(*self.repair_tasks.values(), return_exceptions=True)

    def tools(self) -> list[Tool]:
        from chemistry_agent.automation.job_tools import tools as job_tools
        from chemistry_agent.automation.wait_tools import tools as wait_tools
        from chemistry_agent.platform.artifacts import ArtifactRead, ArtifactRegister
        from chemistry_agent.platform.steps import StepReport

        return [
            *job_tools(self),
            *wait_tools(self),
            Tool(
                "calculation_step_report",
                "Report an agent-owned task conclusion with expected_revision, result, reason "
                "and accepted_jobs. One task can own many adopted jobs and failed attempts. "
                "Does not change scheduler outcomes. Finish/cancel active linked jobs first.",
                StepReport,
                self.platform.handler("plan.report"),
                True,
            ),
            Tool(
                "calculation_artifact_register",
                "Snapshot a raw job file into immutable platform storage. A stable key makes "
                "registration idempotent. Use returned artifact_id as a file dependency.",
                ArtifactRegister,
                self.platform.handler("artifact.register"),
                True,
            ),
            Tool(
                "calculation_artifact_read",
                "List, inspect or read immutable file snapshots.",
                ArtifactRead,
                self.platform.handler("artifact.read"),
                True,
            ),
            Tool(
                "calculation_plan_extend",
                "Append agent-designed steps to this auto-authorized "
                "plan during a job completion turn. Existing nodes and results remain unchanged. "
                "Stable node IDs deduplicate repeated requests. File dependencies may reference "
                "earlier nodes.",
                PlanExtension,
                self.platform.handler("plan.extend"),
                True,
            ),
            Tool(
                "calculation_plan_stop",
                "Stop this session plan and its submitted jobs on explicit user request. "
                "Repeat to verify remaining jobs. Preserve results.",
                PlanStop,
                self.platform.handler("plan.stop"),
                True,
            ),
            Tool(
                "calculation_plan_save",
                "Create or revise a general executable calculation DAG. "
                "An explicit /auto QUEUE user header starts it immediately. Otherwise draft. "
                "Nodes: task (agent-owned research goal; execution_host; no fixed script), "
                "job (arbitrary Slurm job.script, host, resources, text files, retrieve, "
                "required_outputs; no AiiDA needed), model (ASE Python writes structure.vasp), "
                "vasp (registered "
                "protocol plus overrides), analysis (Python writes result.json; passed=false "
                "blocks descendants). inputs map aliases to structure_id OR upstream node/output. "
                "Code reads alias.vasp and inputs.json (alias: {result} or {structure_id,file}). "
                'analysis.result_files stages any relative file paths (or ["*"] for all). '
                "file_source selects retrieved or remote. inputs.json alias.files maps names "
                "to local paths; no file-type whitelist. "
                "files accepts {node OR job_id OR file_id, path, target, source}; files are staged "
                "at target and described in file_inputs.json. File references imply dependencies. "
                "For job nodes use srun/mpirun in script as needed, never sbatch a detached child. "
                "Job completion wakes the agent with tools in the original conversation. "
                "Dependencies include all input references. Branches execute concurrently after "
                "authorization (auto mode or panel confirmation). Revisions require plan_id and "
                "expected_revision; confirmed plans are immutable. Use this for new multi-step "
                "research workflows, not just a prose plan. Set group_path on repeated branches, "
                "e.g. [material, adsorbate, initial_site] or [method, scan_value]. Shared inputs "
                "and cross-branch results belong at their common level. Only depends_on and "
                "input references determine execution.",
                PlanDraft,
                self.platform.handler("plan.save"),
                True,
            ),
            Tool(
                "calculation_plan_program",
                "For a large or repetitive workflow, write a Python program (stdlib, ASE, numpy) "
                "that generates the entire calculation_plan_save JSON document in result.json. "
                "Use loops/functions for site, facet, molecule and frequency branches; the "
                "expanded graph is validated, displayed and executed like any other plan. "
                "Avoid nested code strings: import runpy; "
                "W=runpy.run_path('plan_writer.py')['PlanWriter']; "
                "p=W(title,goal); define standalone normal functions with imports inside. "
                "p.model(id,fn,inputs={},arguments={}); p.vasp(id,source_id,protocol_id,incar=...,"
                "resources=...,kmesh=...,require_ionic=...); p.analysis(id,fn,inputs=...,"
                "result_files=['OUTCAR']); all three accept group_path=[outer_label, inner_label]. "
                "Group repeated candidates by their meaningful dimensions, such as adsorbate then "
                "initial site. Single-source descendants inherit the path for display. "
                "p.ref(id,'structure' or 'result'); p.write(). "
                "Use previous_id to stage a saved generator as previous.py: patch or reuse its "
                "source in Python and execute the corrected program to emit result.json. For an "
                "unterminated multiline literal you can opt in to the helper: import runpy; "
                "from pathlib import Path; h=runpy.run_path('plan_writer.py'); "
                "exec(h['repair_literal_newlines'](Path('previous.py').read_text())).",
                PlanProgram,
                self.compile_program,
                True,
            ),
            Tool(
                "calculation_plan_source",
                "List saved plan programs or read their source lines, "
                "including failed attempts. Use previous_id in calculation_plan_program to repair "
                "without regenerating the entire workflow.",
                ProgramSource,
                self.program_source,
                True,
            ),
            Tool(
                "calculation_plan_repair",
                "During automatic fault handling replace one failed node in its existing plan. "
                "Keep its ID, kind and upstream dependencies; supply the complete corrected node "
                "and a reason, or pass node_id to retain settings. For a timed-out relaxation, "
                "restart_from_last_structure=true reuses its own saved geometry and constraints. "
                "Supply original inputs/dependencies. If a failed analysis needs a "
                "better geometry or frequencies, pass rerun_upstream=[{node_id: relax_id, "
                "use_final_structure: true, incar: {...}}, {node_id: freq_id, incar: {...}}]. "
                "Clones that completed input chain with the original queue and resources; "
                "only this analysis consumes the retries. Original jobs remain intact.",
                NodeRepair,
                self.platform.handler("plan.repair"),
                True,
            ),
            Tool(
                "calculation_plan_plot",
                "Attach one Python figure step to an existing plan. It reads the upstream "
                "result from inputs.json ['data']['result'] when ready, even for running or "
                "completed workflows. Use matplotlib to save a 300 dpi PNG with title and "
                "axis labels/units and write result.json. No new cluster job. The image is "
                "shown inline in the plan and completion message.",
                PlanFigure,
                self.add_figure,
                True,
            ),
            Tool(
                "calculation_plan_get",
                "Read current plan. For large graphs request node_id for exact code/outputs or "
                "full=true for the complete graph only when necessary. Small plans include "
                "step outputs. Omit plan_id to list this session's plans. Reuse completed outputs "
                "as structure_id in a new plan instead of repeating calculations. Before preparing "
                "or submitting a task, read its latest purpose, text_files and submission_script: "
                "users can edit unstarted steps in the UI. Honor those saved inputs instead of stale history.",
                PlanQuery,
                self.platform.handler("plan.get"),
                True,
            ),
        ]
