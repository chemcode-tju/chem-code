"""Versioned task assignments and independent native session bindings."""

from __future__ import annotations

import json
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field

from chemistry_agent.automation.job_models import FileReference, JobSettings
from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.plan_models import PlanDraft, PlanNode
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import OperationContext
from chemistry_agent.store import utc_now

if TYPE_CHECKING:
    from aiosqlite import Connection

    from chemistry_agent.automation.plans import CalculationPlans
    from chemistry_agent.backends.service import BackendRouter, BackendSettings
    from chemistry_agent.store import Store


async def initialize(db: Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS task_agent_presets (
            id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_agent_sessions (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id),
            parent_id TEXT NOT NULL REFERENCES sessions(id),
            plan_id TEXT NOT NULL, node_id TEXT NOT NULL,
            UNIQUE(parent_id,plan_id,node_id)
        );
    """)


class PresetInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    backend: Literal["codex", "antigravity"]
    instance_id: str = Field(min_length=1, max_length=96)
    model: str = Field(min_length=1, max_length=160)
    effort: str = Field(default="", max_length=32)
    instructions: str = Field(default="", max_length=4000)
    enabled: bool = True


class AssignmentInput(StrictModel):
    expected_revision: int = Field(ge=1)
    preset_id: UUID | None
    preset_version: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class TaskAgentScope:
    parent_id: str
    plan_id: str
    node_id: str
    session_id: str


current_task_agent: ContextVar[TaskAgentScope | None] = ContextVar("task_agent_scope", default=None)

TASK_TOOLS = {
    "calculation_plan_get",
    "calculation_job_files",
    "calculation_job_hosts",
    "calculation_step_report",
    "calculation_wait_register",
    "calculation_wait_control",
    "calculation_artifact_register",
    "calculation_artifact_read",
    "calculation_task_submit",
}


class TaskSubmission(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=120)
    job: JobSettings
    files: list[FileReference] = Field(default_factory=list, max_length=80)


async def submit_task_job(
    plans: CalculationPlans, args: TaskSubmission, context: OperationContext
) -> dict:
    """Persist one child job for each approved submission key before contacting Slurm."""
    scope = current_task_agent.get()
    if not scope or context.session_id != scope.parent_id:
        raise AgentError("task_agent_required", "此工具仅供已分配的任务 Agent 使用。", 403)
    identifier = "taskjob_" + uuid5(UUID(scope.session_id), args.key).hex
    async with plans.lock(scope.parent_id, scope.plan_id):
        plan = plans.chat.load(scope.parent_id, "plans", scope.plan_id)
        if plan["status"] != "running" or (scope.parent_id, scope.plan_id) in plans.stop_requests:
            raise AgentError("plan_not_active", "方案当前未执行。", 409)
        task = next(node for node in plan["nodes"] if node["id"] == scope.node_id)
        if args.job.adopted or args.job.host not in plans.slurm.hosts():
            raise AgentError("task_job_invalid", "请使用已登记主机提交新作业。", 422)
        if task.get("execution_host") and args.job.host != task["execution_host"]:
            raise AgentError("task_job_host", "请使用此任务指定的计算主机。", 422)
        if plan.get("auto_queue") and args.job.resources.queue != plan["auto_queue"]:
            raise AgentError("wrong_queue", "请使用方案已授权的队列。", 422)
        dependencies = sorted(set(task["depends_on"]) | {f.node for f in args.files if f.node})
        node = PlanNode(
            id=identifier,
            title=args.title,
            kind="job",
            parent_task=scope.node_id,
            depends_on=dependencies,
            job=args.job,
            files=args.files,
            inputs=task.get("inputs", {}),
        )
        proposed = node.stored_record()
        existing = next((n for n in plan["nodes"] if n["id"] == identifier), None)
        if existing:
            if existing != proposed:
                raise AgentError("task_submission_conflict", "此提交 key 已用于另一组输入。", 409)
            return {"node_id": identifier, "status": plan["runtime"][identifier]["status"]}
        if plan["runtime"][scope.node_id]["status"] not in {"pending", "running"} or any(
            plan["runtime"][dep]["status"] != "completed" for dep in task["depends_on"]
        ):
            raise AgentError("task_not_ready", "此任务当前不能提交作业。", 409)
        if any(
            dep not in plan["runtime"] or plan["runtime"][dep]["status"] != "completed"
            for dep in dependencies
        ):
            raise AgentError("dependency_pending", "请等待作业输入依赖完成。", 409)
        existing_nodes = [
            {key: value for key, value in item.items() if key in PlanNode.model_fields}
            for item in plan["nodes"]
        ]
        PlanDraft(title=plan["title"], goal=plan["goal"], nodes=[*existing_nodes, proposed])
        plans.chat.save(scope.parent_id, "plan_versions", str(uuid4()), plan)
        plan["nodes"].append(proposed)
        plan["runtime"][identifier] = {"status": "pending"}
        plan["runtime"][scope.node_id]["status"] = "running"
        plan.update(revision=plan["revision"] + 1, changed_nodes=[identifier])
        plan["history"].append(
            {
                "revision": plan["revision"],
                "changed_nodes": [identifier],
                "removed_nodes": [],
                "source": "task_agent_submission",
                "run_id": context.run_id,
                "at": utc_now(),
            }
        )
        plans.save(scope.parent_id, plan)
        return {"node_id": identifier, "status": "pending"}


def task_session(parent: str, plan: str, node: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"chem-agent:task:{parent}:{plan}:{node}"))


def task_target(plan: dict, event: dict) -> str | None:
    node = event.get("parent_task") or event["node_id"]
    return node if node in plan.get("task_agents", {}) else None


def delivery_target(plan: dict, payload: dict) -> str | None:
    return payload.get("task_node_id") or task_target(plan, payload["events"][0])


async def presets(store: Store) -> list[dict]:
    async with store.lock:
        rows = await (
            await store.db.execute("SELECT * FROM task_agent_presets ORDER BY id")
        ).fetchall()
    return [
        json.loads(row["payload"]) | {"id": row["id"], "version": row["version"]} for row in rows
    ]


async def save_preset(
    router: BackendRouter, identifier: str, body: PresetInput, catalog: dict | None
) -> dict:
    if not body.name.strip():
        raise AgentError("preset_name_required", "请填写预设名称。", 422)
    if body.enabled:
        model = next(
            (entry for entry in (catalog or {}).get("models", []) if entry["id"] == body.model),
            None,
        )
        if not model or (
            body.effort
            and body.effort
            not in {e["id"] if isinstance(e, dict) else e for e in model.get("efforts", [])}
        ):
            raise AgentError("preset_model_unavailable", "此实例未提供所选模型或推理强度。", 422)
    store = router.store
    body = body.model_copy(update={"name": body.name.strip()})
    async with store.transaction():
        await store.db.execute(
            "INSERT INTO task_agent_presets VALUES (?,1,?) ON CONFLICT(id) DO UPDATE SET "
            "version=version+1,payload=excluded.payload WHERE payload<>excluded.payload",
            (identifier, body.model_dump_json()),
        )
        row = await store._one("SELECT version FROM task_agent_presets WHERE id=?", (identifier,))
    return body.model_dump() | {"id": identifier, "version": row["version"]}


async def assign(
    plans: CalculationPlans,
    router: BackendRouter,
    parent: str,
    identifier: str,
    node_id: str,
    change: AssignmentInput,
) -> dict:
    async with plans.lock(parent, identifier):
        plan = plans.chat.load(parent, "plans", identifier)
        plans.require_current_backend(plan)
        if plan["revision"] != change.expected_revision:
            raise AgentError("plan_revision_conflict", "方案已更新，请核对后重新选择。", 409)
        if plan["status"] != "draft":
            raise AgentError("task_assignment_locked", "方案确认后，任务执行配置已锁定。", 409)
        node = next((n for n in plan["nodes"] if n["id"] == node_id), None)
        if not node or node["kind"] != "task":
            raise AgentError("task_assignment_invalid", "请为 Agent 负责的研究步骤选择预设。", 422)
        configured = plan.get("task_agents", {})
        previous = deepcopy(plan)
        if change.preset_id:
            preset = next(
                (
                    p
                    for p in await presets(router.store)
                    if p["id"] == str(change.preset_id) and p["enabled"]
                ),
                None,
            )
            if not preset:
                raise AgentError("task_preset_unavailable", "此执行预设已停用或不存在。", 409)
            if change.preset_version is not None and preset["version"] != change.preset_version:
                raise AgentError(
                    "task_preset_changed", "执行预设已有更新，请核对模型和推理强度后再选择。", 409
                )
            await resolve(router, preset)
            configured[node_id] = preset
        else:
            configured.pop(node_id, None)
        if configured == previous.get("task_agents", {}):
            return plans.public(plan)
        plans.chat.save(parent, "plan_versions", str(uuid4()), previous)
        plan.update(task_agents=configured, revision=plan["revision"] + 1, changed_nodes=[node_id])
        plan["history"].append(
            {
                "revision": plan["revision"],
                "changed_nodes": [node_id],
                "removed_nodes": [],
                "source": "task_agent_assignment",
                "at": utc_now(),
            }
        )
        plans.save(parent, plan)
        return plans.public(plan)


async def resolve(router: BackendRouter, preset: dict) -> BackendSettings:
    from chemistry_agent.backends.service import SessionSelection

    settings = await router.settings()
    choice = SessionSelection(
        backend=preset["backend"], instance_id=preset["instance_id"], permission="user"
    )
    instance = router.hosting.resolve(choice, settings)
    backend = preset["backend"]
    return settings.model_copy(
        update=instance
        | {
            "working_directory": "",
            "workspace_id": "",
            "workspace_path": "",
            f"{backend}_model": preset["model"],
            f"{backend}_effort": preset["effort"],
            "permission_mode": "user",
            "codex_sandbox": "workspace-write",
        }
    )


async def ensure_session(
    router: BackendRouter, parent: str, plan: dict, node: str, owner_id: str | None
) -> str:
    """Allocate session and immutable binding together before starting a native thread."""
    store = router.store
    session = task_session(parent, plan["id"], node)
    preset = plan["task_agents"][node]
    config = await resolve(router, preset)
    async with store.transaction():
        existing = await store._one(
            "SELECT b.config FROM task_agent_sessions t JOIN agent_bindings b USING(session_id) "
            "WHERE t.session_id=?",
            (session,),
        )
        if existing:
            if json.loads(existing["config"])["host_id"] != config.host_id:
                raise AgentError(
                    "task_host_changed", "Agent 实例的主机已变化，无法恢复原任务线程。", 409
                )
            return session
        now = utc_now()
        title = next(n["title"] for n in plan["nodes"] if n["id"] == node)
        await store.db.execute(
            "INSERT INTO sessions(id,title,created_at,updated_at,owner_id) VALUES (?,?,?,?,?)",
            (session, title, now, now, owner_id),
        )
        await store.db.execute(
            "INSERT INTO task_agent_sessions VALUES (?,?,?,?)", (session, parent, plan["id"], node)
        )
        await store.db.execute(
            "INSERT INTO agent_bindings(session_id,backend,config) VALUES (?,?,?)",
            (session, preset["backend"], config.model_dump_json()),
        )
    return session
