"""Durable, user-approved DAGs composed by the conversation agent.

The web worker owns graph scheduling; AiiDA owns each remote calculation. Every
submission is checkpointed by preview ID before the idempotent AiiDA submit call.
"""

import asyncio
import contextlib
import io
import json
import logging
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ase.io import read

from chemistry_agent.automation.configuration import CalculationConfiguration, CalculationProtocol
from chemistry_agent.automation.constraints import poscar_text, restore_constraints
from chemistry_agent.automation.events import utc_now
from chemistry_agent.automation.modeling import execute_model
from chemistry_agent.automation.models import StructureInput
from chemistry_agent.automation.plan_authority import auto_queue, auto_request
from chemistry_agent.automation.plan_models import NodeRepair, PlanDraft, PlanProgram, PlanQuery
from chemistry_agent.automation.plan_repair import diagnose, eligible, replace_node
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "blocked"}


def generated_files(directory: Path, aliases: set[str]) -> list[str]:
    """Expose generated artifacts, excluding staged inputs and interpreter bookkeeping."""
    excluded = {"inputs.json", "model.py", "sandbox.sb", "execution.log"}
    excluded.update(f"{alias}.{suffix}" for alias in aliases for suffix in ("vasp", "extxyz"))
    files = []
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            relative = path.relative_to(directory)
            if (
                relative.parts[0] in aliases
                or path.name == "archive"
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
        self.locks: dict[str, asyncio.Lock] = {}
        self.task: asyncio.Task | None = None
        self.store = None
        self.agent = self.provider = None
        self.repair_tasks: dict[tuple[str, str], asyncio.Task] = {}

    def lock(self, session: str, plan: str) -> asyncio.Lock:
        return self.locks.setdefault(f"{session}/{plan}", asyncio.Lock())

    def save(self, session: str, plan: dict) -> None:
        plan["updated_at"] = utc_now()
        self.chat.save(session, "plans", plan["id"], plan)

    @staticmethod
    def public(plan: dict) -> dict:
        # Execution snapshots are retained privately; effective inputs remain reviewable.
        return {key: value for key, value in plan.items() if key != "snapshots"}

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
                for reference in node.inputs.values():
                    if reference.structure_id:
                        self.chat.load(session, "structures", str(reference.structure_id))
                if node.calculation:
                    settings = node.calculation
                    protocol = CalculationProtocol.model_validate(
                        configuration.get("protocols", settings.protocol_id)
                    ).model_dump(mode="json")
                    protocol["incar"] = {k.upper(): v for k, v in protocol["incar"].items()}
                    protocol["incar"].update({k.upper(): v for k, v in settings.incar.items()})
                    if settings.kmesh:
                        protocol.update(kmesh=list(settings.kmesh), kpoint_spacing=None)
                    if settings.resources:
                        protocol["resources"] = settings.resources.model_dump(mode="json")
                    source = configuration.get("sources", protocol["potential_source_id"])
                    if queue and protocol["resources"]["queue"] != queue:
                        raise AgentError("wrong_queue", f"本轮只授权 {queue} 队列。", 422)
                    key = (protocol["server_id"], protocol["code_id"])
                    if key not in identities:
                        identities[key] = await self.chat.service.call("plan_identity", protocol)
                    identity = identities[key]
                    snapshots[node.id] = {"protocol": protocol, "source": source, **identity}
            nodes = [node.model_dump(mode="json") for node in args.nodes]
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
            previous = {node["id"]: node for node in old["nodes"]} if old else {}
            changed = [node["id"] for node in nodes if previous.get(node["id"]) != node]
            removed = sorted(previous.keys() - {node["id"] for node in nodes})
            revision = old["revision"] + 1 if old else 1
            plan = {
                "id": identifier,
                "revision": revision,
                "title": args.title,
                "goal": args.goal,
                "status": "running" if queue else "draft",
                "max_parallel": None if queue else args.max_parallel,
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
            return self.public(plan)

    async def repair_node(self, args: NodeRepair, context: ToolContext) -> dict:
        return await replace_node(self, args, context)

    async def compile_program(self, args: PlanProgram, context: ToolContext) -> dict:
        """Expand model-written loops locally so repeated branches do not consume context."""
        directory = self.chat.directory(context.session_id, "plan_programs") / str(uuid4())
        directory.mkdir(mode=0o700)
        content = await execute_model(directory, args.code, output_format="json")
        plan = await self.draft(PlanDraft.model_validate_json(content), context)
        return {k: plan[k] for k in ("id", "title", "revision", "status", "max_parallel")} | {
            "nodes": len(plan["nodes"]),
            "calculations": sum(n["kind"] == "vasp" for n in plan["nodes"]),
        }

    async def query(self, args: PlanQuery, context: ToolContext) -> dict:
        if args.plan_id:
            return self.public(self.chat.load(context.session_id, "plans", str(args.plan_id)))
        return {
            "plans": [
                {
                    key: row[key]
                    for key in ("id", "revision", "title", "status", "runtime", "updated_at")
                }
                for row in self.records(context.session_id)
            ]
        }

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
            return {"structure_id": imported["id"], "result": self.chat.structure_summary(imported)}
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
                "analysis_gate_failed", str(result.get("reason", "分析检查未通过。")), 422
            )
        return {"result": result, "files": generated_files(directory, set(inputs))}

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
            state.update(preview_id=preview["preview_id"], input_structure_id=structure_id)
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

    async def inspect_vasp(self, session: str, node: dict, state: dict) -> None:
        from chemistry_agent.automation.chat import JobInput

        job = await self.chat.service.call("job_details", {"job_id": state["job_id"]})
        state["job"] = job
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
        state.update(status="completed", outputs=outputs, completed_at=utc_now())

    async def tick_plan(self, session: str, identifier: str) -> None:
        async with self.lock(session, identifier):
            plan = self.chat.load(session, "plans", identifier)
            if plan["status"] in {"running", "paused"}:
                for node in plan["nodes"]:
                    state = plan["runtime"][node["id"]]
                    try:
                        if state.get("job_id") and state["status"] == "running":
                            await self.inspect_vasp(session, node, state)
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
                        if state.get("job_id"):
                            continue
                        active = sum(
                            bool(s.get("job_id")) and s["status"] == "running"
                            for s in plan["runtime"].values()
                        )
                        if (
                            node["kind"] == "vasp"
                            and plan.get("max_parallel")
                            and active >= plan["max_parallel"]
                        ):
                            continue
                        state.update(
                            status="preparing", started_at=state.get("started_at", utc_now())
                        )
                        self.save(session, plan)
                        if node["kind"] == "vasp":
                            await self.start_vasp(session, plan, node, state)
                        else:
                            outputs = await self.run_code(session, plan, node, state)
                            state.update(
                                status="completed", outputs=outputs, completed_at=utc_now()
                            )
                        state.pop("sync_error", None)
                    except AgentError as exc:
                        # Query/network outages are not calculation failures. Keep tracking the job.
                        if state.get("job_id") and exc.status_code >= 500:
                            state["sync_error"] = exc.message
                        else:
                            state.update(status="failed", error=exc.message, error_code=exc.code)
                    except (ValueError, OSError) as exc:
                        state.update(status="failed", error=str(exc))
                    self.save(session, plan)
                states = [state["status"] for state in plan["runtime"].values()]
                if all(status in TERMINAL for status in states):
                    plan["status"] = (
                        "completed" if all(s == "completed" for s in states) else "failed"
                    )
                    plan["completed_at"] = utc_now()
                self.save(session, plan)
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
                    plan["reported"] = True
                    self.save(session, plan)

            key = (session, identifier)
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

    def start(self, store, agent=None, provider=None) -> None:
        self.store, self.agent, self.provider = store, agent, provider
        self.task = asyncio.create_task(self.watch())

    async def close(self) -> None:
        for task in self.repair_tasks.values():
            task.cancel()
        await asyncio.gather(*self.repair_tasks.values(), return_exceptions=True)
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "calculation_plan_save",
                "Create or revise a general executable calculation DAG. "
                "An explicit /auto QUEUE user header starts it immediately. Otherwise draft. "
                "Nodes: model (ASE Python writes structure.vasp), vasp (registered "
                "protocol plus overrides), analysis (Python writes result.json; passed=false "
                "blocks descendants). inputs map aliases to structure_id OR upstream node/output. "
                "Code reads alias.vasp and inputs.json (alias: {result} or {structure_id,file}). "
                'analysis.result_files stages any relative file paths (or ["*"] for all). '
                "file_source selects retrieved or remote. inputs.json alias.files maps names "
                "to local paths; no file-type whitelist. "
                "Dependencies include all input references. Branches execute concurrently after "
                "authorization (auto mode or panel confirmation). Revisions require plan_id and "
                "expected_revision; confirmed plans are immutable. Use this for new multi-step "
                "research workflows, not just a prose plan.",
                PlanDraft,
                self.draft,
                True,
            ),
            Tool(
                "calculation_plan_program",
                "For a large or repetitive workflow, write a Python program (stdlib, ASE, numpy) "
                "that generates the entire calculation_plan_save JSON document in result.json. "
                "Use loops/functions for site, facet, molecule and frequency branches; the "
                "expanded graph is validated, displayed and executed like any other plan. "
                "When /auto is active omit max_parallel. Return only compact metadata.",
                PlanProgram,
                self.compile_program,
                True,
            ),
            Tool(
                "calculation_plan_repair",
                "During automatic fault handling replace one failed node in its existing plan. "
                "Keep its ID, kind and upstream dependencies; supply the complete corrected node "
                "and a reason. Preserves completed branches; resumes its downstream steps.",
                NodeRepair,
                self.repair_node,
                True,
            ),
            Tool(
                "calculation_plan_get",
                "Read current executable plan, exact code/parameters and "
                "step outputs. Omit plan_id to list this session's plans. Reuse completed outputs "
                "as structure_id in a new plan instead of repeating calculations.",
                PlanQuery,
                self.query,
                True,
            ),
        ]
