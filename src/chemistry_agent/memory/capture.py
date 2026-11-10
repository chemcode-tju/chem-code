"""Deterministic capture from persisted calculation artifacts, never from chat claims."""

import json
import logging
from typing import TYPE_CHECKING, Any

from chemistry_agent.memory.models import MemoryDocument, parameter_diff
from chemistry_agent.memory.service import ResearchMemory

if TYPE_CHECKING:
    from chemistry_agent.store import Store

logger = logging.getLogger(__name__)


def capture_document(**fields: Any) -> MemoryDocument:
    """Bound indexed prose; explicitly retain source pointers for very large artifacts."""
    content = fields["content"]
    if len(content) > 80000 or len(content.encode()) > 160000:
        fields["content"] = (
            "[大型源记录摘录，不是完整方案或结果。请从 evidence 中的来源会话、"
            "plan_id/node_id 或 job_id 读取完整原始记录。]\n\n"
            + content[:78000].encode()[:156000].decode("utf-8", errors="ignore")
            + "\n\n[摘录到此结束；后文未进入记忆索引，不能视为不存在。]"
        )
    return MemoryDocument(**fields)


def calculation_evidence(job: dict, calculation: dict) -> dict:
    """A finished scheduler job is weaker evidence than parsed convergence."""
    status = job.get("results", {}).get("run_status", {})
    electronic = status.get("electronic_converged")
    ionic = status.get("ionic_converged")
    require_ionic = calculation.get("require_ionic", False)
    converged = bool(
        job.get("finished_ok") and electronic is True and (not require_ionic or ionic is True)
    )
    return {
        "level": "converged" if converged else "observed",
        "job_id": job.get("id"),
        "pk": job.get("pk"),
        "scheduler_job_id": job.get("scheduler_job_id"),
        "process_state": job.get("process_state"),
        "exit_status": job.get("exit_status"),
        "electronic_converged": electronic,
        "ionic_converged": ionic,
        "require_ionic": require_ionic,
        "scientifically_validated": False,
    }


def node_parameters(node: dict, snapshot: dict) -> dict:
    protocol = snapshot.get("protocol", {})
    # Allowlist: no SSH connection, secret, or environment configuration is copied.
    result = {
        key: protocol[key]
        for key in (
            "incar",
            "kmesh",
            "kpoint_spacing",
            "potential_mapping",
            "potential_source_id",
            "resources",
            "code_id",
            "server_id",
        )
        if key in protocol
    }
    result.update(node.get("effective", {}))
    if snapshot.get("source", {}).get("family"):
        result["potential_family"] = snapshot["source"]["family"]
    if not result:
        result = node.get("calculation") or {}
    return result


async def capture_plan(memory: ResearchMemory, session: str, plan: dict) -> None:
    """Idempotent snapshots and failure chains; no calls to a model or remote scheduler."""
    systems, tasks = plan.get("research_systems", []), plan.get("research_tasks", [])
    source = {
        "level": "observed",
        "plan_id": plan["id"],
        "plan_revision": plan["revision"],
        "status": plan["status"],
        "scientifically_validated": False,
    }
    # Persist executable code and inputs so the document is useful outside a transcript.
    body = {key: plan[key] for key in ("title", "goal", "nodes")}
    content = (
        f"{plan['goal']}\n\n状态：{plan['status']}。方案及执行节点：\n\n```json\n"
        + json.dumps(body, ensure_ascii=False, indent=2)
        + "\n```"
    )
    await memory.save(
        session,
        capture_document(
            title=plan["title"],
            kind="plan",
            content=content,
            systems=systems,
            tasks=tasks,
            applicability="方案记录可用于设计；完成状态不代表已做精度测试或科学验证。",
        ),
        source_key=f"plan:{plan['id']}",
        evidence=source,
    )
    for node in plan["nodes"]:
        state = plan.get("runtime", {}).get(node["id"], {})
        if state.get("status") not in {"completed", "failed"}:
            continue
        job = state.get("job", {})
        parameters = node_parameters(node, plan.get("snapshots", {}).get(node["id"], {}))
        if state.get("input_structure_fingerprint"):
            parameters["structure_fingerprint"] = state["input_structure_fingerprint"]
        evidence = calculation_evidence(job, node.get("calculation") or {}) | {
            "plan_id": plan["id"],
            "plan_revision": plan["revision"],
            "node_id": node["id"],
            "status": state["status"],
            "input_structure_id": state.get("input_structure_id"),
            "preview_id": state.get("preview_id"),
        }
        details = {
            key: state[key]
            for key in (
                "status",
                "error",
                "error_code",
                "outputs",
                "input_structure_id",
            )
            if key in state
        }
        details["run_status"] = job.get("results", {}).get("run_status", {})
        details["exit_message"] = job.get("exit_message")
        await memory.save(
            session,
            capture_document(
                title=f"{plan['title']} / {node['title']}"[:200],
                kind="calculation",
                content=json.dumps(details, ensure_ascii=False, indent=2),
                systems=systems,
                tasks=tasks,
                parameters=parameters,
                applicability="仅供类似体系参数排查；结构、泛函、赝势、约束、环境仍需逐项核对。",
            ),
            source_key=f"node:{plan['id']}:{node['id']}",
            evidence=evidence,
        )
        attempts = state.get("attempts", [])
        for number, attempt in enumerate(attempts, 1):
            # Link each failure to its immediate retry, not all failures to the final success.
            following = attempts[number] if number < len(attempts) else None
            after_node = following["node"] if following else node
            after_state = following["state"] if following else state
            after_parameters = node_parameters(after_node, {})
            after_evidence = calculation_evidence(
                after_state.get("job", {}),
                after_node.get("calculation") or {},
            ) | {
                "plan_id": plan["id"],
                "node_id": node["id"],
                "status": after_state["status"],
                "attempt": number,
            }
            before = node_parameters(attempt["node"], {})
            old_state = attempt.get("state", {})
            lesson = {
                "original_error": old_state.get("error"),
                "original_error_code": old_state.get("error_code"),
                "original_job_id": old_state.get("job_id"),
                "proposed_reason": attempt.get("reason"),
                "parameter_changes": parameter_diff(before, after_parameters),
                "before_node": attempt["node"],
                "after_node": after_node,
                "retry_outcome": after_state["status"],
                "causality": "记录修改后观察到的结果；多次修改不能证明其中某一项是根因。",
            }
            await memory.save(
                session,
                capture_document(
                    title=f"故障经验：{plan['title']} / {node['title']} / {number}"[:200],
                    kind="lesson",
                    content=json.dumps(lesson, ensure_ascii=False, indent=2),
                    systems=systems,
                    tasks=tasks,
                    parameters=after_parameters,
                    applicability="修复理由是当时的假设；仅有后续收敛证据才可称重算收敛。",
                ),
                source_key=f"lesson:{plan['id']}:{node['id']}:{number}",
                evidence=after_evidence,
            )


async def safely_capture_plan(store: "Store | None", session: str, plan: dict) -> None:
    """Memory indexing failures must never cause a successful calculation to be resubmitted."""
    if store is None or not getattr(store, "research_memory_enabled", True):
        return
    try:
        await capture_plan(ResearchMemory(store), session, plan)
    except Exception:
        logger.exception("Research memory capture deferred for plan %s", plan["id"])


async def capture_job(store: "Store | None", session: str, artifact: dict, job: dict) -> None:
    """Capture the legacy single-job route as well as DAG calculations."""
    if store is None or not getattr(store, "research_memory_enabled", True):
        return
    if artifact.get("plan_id") or job.get("process_state") not in {
        "finished",
        "excepted",
        "killed",
    }:
        return
    request = artifact.get("request", {})
    parameters = {
        key: request[key]
        for key in (
            "incar",
            "kmesh",
            "potential_family",
            "potential_mapping",
            "resources",
            "code_id",
        )
        if key in request
    }
    evidence = calculation_evidence(
        job,
        {
            "require_ionic": bool(
                request.get("incar", {}).get("nsw", request.get("incar", {}).get("NSW", 0))
            ),
        },
    ) | {"preview_id": artifact["preview_id"], "input_structure_id": artifact.get("structure_id")}
    details = {
        key: job[key]
        for key in (
            "label",
            "results",
            "exit_message",
            "recent_process_errors",
            "finished_ok",
        )
        if key in job
    }
    try:
        memory = ResearchMemory(store)
        await memory.save(
            session,
            capture_document(
                title=f"计算记录：{job.get('label') or job['id']}"[:200],
                kind="calculation",
                content=json.dumps(details, ensure_ascii=False, indent=2),
                parameters=parameters,
                applicability="旧单任务记录；体系标识不完整时，先读取源结构和预览再复用。",
            ),
            source_key=f"job:{job['id']}",
            evidence=evidence,
        )
        if artifact.get("recovery_source_job_id"):
            lesson = {
                "source_job_id": artifact["recovery_source_job_id"],
                "source_preview_id": artifact.get("recovery_source_preview_id"),
                "original_error": artifact.get("recovery_source_error"),
                "proposed_reason": artifact.get("recovery_reason"),
                "parameter_changes": parameter_diff(
                    artifact.get("recovery_before_parameters", {}),
                    parameters,
                ),
                "retry_result": details,
            }
            await memory.save(
                session,
                capture_document(
                    title=f"故障重算：{job.get('label') or job['id']}"[:200],
                    kind="lesson",
                    content=json.dumps(lesson, ensure_ascii=False, indent=2),
                    parameters=parameters,
                    applicability="单任务恢复的相邻重算记录；修复理由不是已证实根因。"
                    "工作流子步骤的实际参数仍须查原始作业。",
                ),
                source_key=f"retry:{job['id']}",
                evidence=evidence,
            )
    except Exception:
        logger.exception("Research memory capture deferred for job %s", job["id"])
