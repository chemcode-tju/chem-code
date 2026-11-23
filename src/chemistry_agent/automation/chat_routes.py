"""Browser confirmation is authoritative; no LLM tool can submit a job."""

import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response

from chemistry_agent.automation.chat import (
    CalculationChat,
    ImportInput,
)
from chemistry_agent.automation.plan_models import PlanConfirmation, PlanControl, PlanDraft
from chemistry_agent.automation.routes import local_service
from chemistry_agent.automation.step_edit import StepEdit, edit_policy, edit_step
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.attention import pending_actions, reconcile_job
from chemistry_agent.platform.results import ResultInspect, ResultRegister
from chemistry_agent.platform.task_agents import AssignmentInput, assign, presets, task_session
from chemistry_agent.tools import ToolContext

router = APIRouter(prefix="/api/automation/chat", tags=["chat calculations"])


async def session_chat(request: Request, session_id: UUID) -> CalculationChat:
    local_service(request)
    if not request.app.state.settings.automation_enabled:
        raise AgentError("automation_disabled", "请先启用计算平台。", 503)
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    return request.app.state.calculation_chat


@router.post("/{session_id}/structures")
async def upload_structure(session_id: UUID, body: ImportInput, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.import_structure(body, ToolContext(str(session_id), str(uuid4())))


@router.get("/{session_id}/artifacts")
async def artifacts(session_id: UUID, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    identifier = str(session_id)
    monitor = getattr(request.app.state, "task_monitor", None)
    snapshots = monitor.events.snapshots(identifier) if monitor else {}
    return {
        "plans": request.app.state.platform.plans.records(identifier),
        "task_agent_presets": await presets(request.app.state.store),
        "analyses": chat.list_records(identifier, "analyses"),
        "live_jobs": snapshots,
        "monitor": monitor.events.inbox(identifier, limit=1)["monitor"] if monitor else {},
        "recovery": {job_id: monitor.events.recovery(job_id) for job_id in snapshots}
        if monitor
        else {},
        "policies": {
            identifier: monitor.events.policy(job.get("recovery_root_id", identifier))
            for identifier, job in snapshots.items()
        }
        if monitor
        else {},
        "structures": [
            chat.structure_summary(item) for item in chat.list_records(identifier, "structures")
        ],
        "previews": chat.list_records(identifier, "previews"),
        "models": chat.list_records(identifier, "model_records"),
        "submissions": {
            item.stem: json.loads(item.read_text())
            for item in chat.directory(identifier, "submissions").glob("*.json")
        },
    }


@router.get("/{session_id}/plans/{preview_id}/steps/{stage}/{file}")
async def inspect_step(
    session_id: UUID,
    preview_id: UUID,
    stage: str,
    file: Literal["info", "POSCAR", "CONTCAR", "XDATCAR"],
    request: Request,
):
    chat = await session_chat(request, session_id)
    session, identifier = str(session_id), str(preview_id)
    plan = chat.load(session, "previews", identifier)
    stages = plan.get("stages") or [{"stage": "single"}]
    step = next((item for item in stages if item["stage"] == stage), None)
    if not step or stage == "energies":
        raise AgentError("step_not_found", "此方案没有对应的计算步骤。", 404)
    path = chat.directory(session, "submissions") / f"{identifier}.json"
    root = json.loads(path.read_text())["job"]["id"] if path.is_file() else None
    preview = plan["files"].get("POSCAR" if stage == "single" else f"{stage}/POSCAR")
    preview_source = "方案预览"
    if stage == "singlepoint" and not preview:
        preview = plan["files"].get("relax/POSCAR")
        preview_source = "前序初始结构示意，实际输入取自优化结果"
    info = {"files": {}, "points": [], "ionic_step": None, "terminal": False, "note": ""}
    if root or step.get("reused_pk"):
        args = {"root_id": root, "reused_pk": step.get("reused_pk"), "stage": stage, "file": file}
        try:
            info = await chat.service.call("step_data", args)
        except AgentError as exc:
            if file != "POSCAR" or not preview or exc.status_code != 404:
                raise
    if file == "info":
        monitor = getattr(request.app.state, "task_monitor", None)
        live = monitor.events.snapshots(session) if monitor else {}
        investigations = []
        for row in reversed(chat.list_records(session, "investigations")):
            if row["source_preview_id"] != identifier or row["stage"] != stage:
                continue
            ref = row["related_preview_id"]
            submitted = chat.directory(session, "submissions") / f"{ref}.json"
            job = json.loads(submitted.read_text())["job"] if submitted.is_file() else {}
            job = live.get(job.get("id"), job)
            results = job.get("results", {})
            investigations.append(
                {
                    **row,
                    "pk": job.get("pk"),
                    "status": job.get("display_title") or job.get("process_state") or "待确认",
                    "terminal": job.get("process_state") in {"finished", "excepted", "killed"},
                    "electronic_converged": results.get("run_status", {}).get(
                        "electronic_converged"
                    ),
                    "energy_ev": results.get("total_energies", {}).get("energy_extrapolated"),
                }
            )
        info["investigations"] = investigations
        if preview:
            info["files"].setdefault("POSCAR", preview_source)
        return info
    content = info.get("content") or (preview if file == "POSCAR" else None)
    if not content:
        raise AgentError("step_file_missing", "此步骤尚无对应结构。", 404)
    return Response(
        content,
        media_type="text/plain",
        headers={
            "X-Structure-Name": info.get("name", file),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{session_id}/jobs/{job_id}/structure.extxyz")
async def download_output(session_id: UUID, job_id: str, request: Request) -> Response:
    chat = await session_chat(request, session_id)
    data = await chat.output_structure(str(session_id), str(job_id))
    filename = f"job-{int(data['source_job_pk'])}-structure.extxyz"
    return Response(
        data["extxyz"],
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{session_id}/jobs/{job_id}/steps/{child_id}/structure.extxyz")
async def download_step(
    session_id: UUID, job_id: UUID, child_id: UUID, request: Request
) -> Response:
    chat = await session_chat(request, session_id)
    await chat.require_session_job(str(session_id), str(job_id))
    parent = await chat.service.call("job_details", {"job_id": str(job_id)})
    if not any(step["id"] == str(child_id) for step in parent.get("steps", [])):
        raise AgentError("step_not_found", "此步骤不属于所选流程。", 404)
    data = await chat.service.call("job_structure", {"job_id": str(child_id)})
    filename = f"step-{int(data['source_job_pk'])}-structure.extxyz"
    return Response(
        data["extxyz"],
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{session_id}/files/{kind}/{artifact_id}")
async def download(
    session_id: UUID, kind: Literal["structure", "model"], artifact_id: UUID, request: Request
) -> Response:
    chat = await session_chat(request, session_id)
    if kind == "model":
        data = chat.load(str(session_id), "model_records", str(artifact_id))
        content, filename = data["code"], "model.py"
    else:
        data = chat.load(str(session_id), "structures", str(artifact_id))
        content, filename = data["extxyz"], "structure.extxyz"
    return Response(
        content,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{session_id}/progress")
async def calculation_progress(
    session_id: UUID,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    plan_id: UUID | None = None,
    node_id: str | None = Query(None, max_length=120),
) -> dict:
    chat = await session_chat(request, session_id)
    return chat.progress.timeline(
        str(session_id), after, limit, str(plan_id) if plan_id else None, node_id
    )


@router.get("/{session_id}/events")
async def session_events(session_id: UUID, request: Request) -> dict:
    await session_chat(request, session_id)
    monitor = getattr(request.app.state, "task_monitor", None)
    if not monitor:
        return {"events": [], "unread": 0, "jobs": {}, "recovery": {}, "monitor": {}}
    snapshots = monitor.events.snapshots(str(session_id))
    return {
        **monitor.events.inbox(str(session_id)),
        "latest_report": monitor.events.latest_report(str(session_id)),
        "jobs": snapshots,
        "recovery": {job_id: monitor.events.recovery(job_id) for job_id in snapshots},
    }


@router.get("/{session_id}/workflows")
async def workflow_plans(session_id: UUID, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    return {
        "plans": chat.plans.records(str(session_id)),
        "task_agent_presets": await presets(request.app.state.store),
    }


@router.get("/{session_id}/workflows/{plan_id}/artifacts/{node_id}/{filename:path}")
async def workflow_artifact(
    session_id: UUID,
    plan_id: UUID,
    node_id: str,
    filename: str,
    request: Request,
    preview: bool = False,
) -> FileResponse:
    chat = await session_chat(request, session_id)
    plan = chat.load(str(session_id), "plans", str(plan_id))
    files = plan["runtime"].get(node_id, {}).get("outputs", {}).get("files", [])
    root = chat.directory(str(session_id), "plan_runs") / str(plan_id) / node_id
    return result_file(root, files, filename, preview)


@router.get("/{session_id}/analyses/{analysis_id}/artifacts/{filename:path}")
async def analysis_artifact(
    session_id: UUID, analysis_id: UUID, filename: str, request: Request, preview: bool = False
) -> FileResponse:
    chat = await session_chat(request, session_id)
    record = chat.load(str(session_id), "analyses", str(analysis_id))
    root = chat.directory(str(session_id), "plan_runs") / str(analysis_id) / "analysis"
    return result_file(root, record.get("files", []), filename, preview)


def result_file(root: Path, files: list[str], filename: str, preview: bool) -> FileResponse:
    if filename not in files or Path(filename).is_absolute() or ".." in Path(filename).parts:
        raise AgentError("artifact_not_found", "此步骤没有该输出文件。", 404)
    path = root / filename
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise AgentError("artifact_not_found", "输出文件不可用。", 404)
    images = {
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    media = images.get(path.suffix.lower())
    if preview and not media:
        raise AgentError("not_a_figure", "此文件不支持图像预览。", 415)
    return FileResponse(
        path,
        filename=path.name,
        media_type=media if preview else "application/octet-stream",
        content_disposition_type="inline" if preview else "attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
            ),
        },
    )


@router.post("/{session_id}/workflows")
async def save_workflow(session_id: UUID, body: "PlanDraft", request: Request) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.plans.draft(body, ToolContext(str(session_id), str(uuid4())))


@router.post("/{session_id}/workflows/{plan_id}/confirm")
async def confirm_workflow(
    session_id: UUID, plan_id: UUID, body: "PlanConfirmation", request: Request
) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.plans.confirm(str(session_id), str(plan_id), body.revision)


@router.post("/{session_id}/workflows/{plan_id}/control")
async def control_workflow(
    session_id: UUID, plan_id: UUID, body: "PlanControl", request: Request
) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.plans.control(str(session_id), str(plan_id), body.action)


@router.post("/{session_id}/workflows/{plan_id}/steps/{node_id}/edit")
async def update_step_text(
    session_id: UUID, plan_id: UUID, node_id: str, body: StepEdit, request: Request
) -> dict:
    chat = await session_chat(request, session_id)
    return await edit_step(chat.plans, str(session_id), str(plan_id), node_id, body)


@router.put("/{session_id}/workflows/{plan_id}/steps/{node_id}/agent")
async def set_task_agent(
    session_id: UUID, plan_id: UUID, node_id: str, body: AssignmentInput, request: Request
) -> dict:
    chat = await session_chat(request, session_id)
    return await assign(
        chat.plans, request.app.state.agent_backends, str(session_id), str(plan_id), node_id, body,
    )


@router.get("/{session_id}/workflows/{plan_id}/steps/{node_id}/thread")
async def task_thread(session_id: UUID, plan_id: UUID, node_id: str, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    session, plan_id = str(session_id), str(plan_id)
    plan = chat.load(session, "plans", plan_id)
    if node_id not in plan.get("task_agents", {}):
        raise AgentError("task_agent_missing", "此步骤没有分配执行预设。", 404)
    store = request.app.state.store
    identifier = task_session(session, plan_id, node_id)
    async with store.lock:
        row = await store._one(
            "SELECT s.active_run_id,b.external_thread_id,b.turn_state "
            "FROM task_agent_sessions t JOIN sessions s ON s.id=t.session_id "
            "JOIN agent_bindings b ON b.session_id=t.session_id WHERE t.session_id=?",
            (identifier,),
        )
        events = await (await store.db.execute(
            "SELECT id,status,error FROM agent_events WHERE session_id=? AND plan_id=? AND ("
            "json_extract(payload,'$.task_node_id')=? OR EXISTS (SELECT 1 FROM "
            "json_each(payload,'$.events') WHERE COALESCE(json_extract(value,'$.parent_task'),"
            "json_extract(value,'$.node_id'))=?)) ORDER BY created_at DESC LIMIT 10",
            (session, plan_id, node_id, node_id),
        )).fetchall()
        runs = await (await store.db.execute(
            "SELECT id,status,started_at,finished_at,error_code,substr(answer,1,800) AS answer "
            "FROM runs WHERE session_id=? ORDER BY started_at DESC,id LIMIT 20",
            (identifier,),
        )).fetchall()
    data = {"events": [dict(item) for item in events]}
    if row is None:
        return data | {"id": None, "runs": [], "approvals": []}
    return data | dict(row) | {
        "id": identifier,
        "runs": [dict(run) for run in runs],
        "approvals": await request.app.state.agent_backends.approvals.list_pending(identifier),
    }


@router.get("/{session_id}/workflows/{plan_id}/attention")
async def workflow_attention(session_id: UUID, plan_id: UUID, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    session = str(session_id)
    plan = chat.load(session, "plans", str(plan_id))
    return {"items": await pending_actions(chat.plans, session, plan)}


@router.post("/{session_id}/workflows/{plan_id}/steps/{node_id}/jobs/{job_id}/reconcile")
async def reconcile_workflow_job(
    session_id: UUID, plan_id: UUID, node_id: str, job_id: UUID, request: Request
) -> dict:
    chat = await session_chat(request, session_id)
    return await reconcile_job(chat.plans, str(session_id), plan_id, node_id, job_id)


@router.post("/{session_id}/results")
async def register_display_result(session_id: UUID, body: ResultRegister, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.plans.platform.results.register(
        body, ToolContext(str(session_id), str(uuid4()))
    )


@router.get("/{session_id}/workflows/{plan_id}/steps/{node_id}/results")
async def inspect_display_result(
    session_id: UUID,
    plan_id: UUID,
    node_id: str,
    job_id: UUID,
    request: Request,
    key: str | None = None,
    file: str | None = None,
    path_step: int | None = None,
):
    chat = await session_chat(request, session_id)
    result = await chat.plans.platform.results.inspect(
        ResultInspect(
            plan_id=plan_id, node_id=node_id, job_id=job_id, key=key, file=file, path_step=path_step
        ),
        ToolContext(str(session_id), str(uuid4())),
    )
    if file is not None:
        return Response(
            result["content"],
            media_type="text/plain",
            headers={
                "Cache-Control": "no-store",
                "X-Structure-Name": quote(result["name"]),
                "X-Result-Stale": str(result.get("stale", False)).lower(),
            },
        )
    return result


@router.get("/{session_id}/workflows/{plan_id}/steps/{node_id}/{file}")
async def workflow_step(
    session_id: UUID,
    plan_id: UUID,
    node_id: str,
    file: str,
    request: Request,
    job_id: UUID | None = None,
):
    import io

    from ase.io import read

    from chemistry_agent.automation.constraints import poscar_text, restore_constraints

    chat = await session_chat(request, session_id)
    plan = chat.load(str(session_id), "plans", str(plan_id))
    node = next((item for item in plan["nodes"] if item["id"] == node_id), None)
    if not node:
        raise AgentError("step_not_found", "此方案没有该步骤。", 404)
    state = plan["runtime"][node_id]
    if node["kind"] in {"job", "task"}:
        from chemistry_agent.automation.generic_step import inspect_generic_step

        result = await inspect_generic_step(
            chat, str(session_id), plan, node, file, str(job_id) if job_id else None
        )
        if file == "info":
            return result
        return Response(
            result["content"],
            media_type="text/plain",
            headers={
                "Cache-Control": "no-store",
                "X-Structure-Name": result["name"],
            },
        )
    if state.get("preview_id"):
        response = await inspect_step(
            session_id, UUID(state["preview_id"]), "single", file, request
        )
        if file == "info":
            response["editing"] = edit_policy(plan, node)
            response["note"] = (
                state.get("error") or state.get("sync_error") or response.get("note", "")
            )
        return response
    input_id = None
    try:
        resolved = chat.plans.resolved_inputs(str(session_id), plan, node)
        input_id = next(
            (value["structure_id"] for value in resolved.values() if "structure_id" in value), None
        )
    except AgentError:
        pass
    output_id = state.get("outputs", {}).get("structure_id")
    available = {}
    if input_id:
        available["POSCAR"] = "已保存的输入结构"
    if output_id:
        available["CONTCAR"] = "建模输出结构"
    if file == "info":
        return {
            "files": available,
            "points": [],
            "ionic_step": None,
            "terminal": state["status"] in {"completed", "failed", "blocked"},
            "note": state.get("error")
            or ("等待上游完成后生成实际输入结构。" if not available else ""),
            "outputs": state.get("outputs", {}),
            "editing": edit_policy(plan, node),
        }
    identifier = output_id if file == "CONTCAR" else input_id if file == "POSCAR" else None
    if not identifier:
        raise AgentError("step_file_missing", "此步骤尚无对应结构。", 404)
    structure = chat.load(str(session_id), "structures", identifier)
    atoms = read(io.StringIO(structure["extxyz"]), format="extxyz")
    restore_constraints(atoms)
    return Response(
        poscar_text(atoms), media_type="text/plain", headers={"Cache-Control": "no-store"}
    )
