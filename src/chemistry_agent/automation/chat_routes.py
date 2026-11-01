"""Browser confirmation is authoritative; no LLM tool can submit a job."""

import json
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from chemistry_agent.automation.chat import (
    CalculationChat,
    ImportInput,
    InvestigationInput,
    JobInput,
)
from chemistry_agent.automation.recovery import RecoveryPolicy
from chemistry_agent.automation.routes import local_service
from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import ChatResponse, TokenUsage, ToolExecution
from chemistry_agent.tools import ToolContext

router = APIRouter(prefix="/api/automation/chat", tags=["chat calculations"])


async def session_chat(request: Request, session_id: UUID) -> CalculationChat:
    local_service(request)
    if not request.app.state.settings.automation_enabled:
        raise AgentError("automation_disabled", "请先启用 AiiDA 自动化。", 503)
    await request.app.state.store.get_session(str(session_id), request.state.user_id)
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


@router.post("/{session_id}/investigations")
async def link_investigation(session_id: UUID, body: InvestigationInput, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.link_investigation(body, ToolContext(str(session_id), str(uuid4())))


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


@router.post("/{session_id}/jobs/{job_id}/structure")
async def import_result(session_id: UUID, job_id: str, request: Request) -> dict:
    chat = await session_chat(request, session_id)
    return await chat.import_job_structure(
        JobInput(job_id=job_id), ToolContext(str(session_id), str(uuid4()))
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


@router.post("/{session_id}/confirm/{preview_id}", response_model=ChatResponse)
async def confirm(
    session_id: UUID,
    preview_id: UUID,
    request: Request,
    policy: Annotated[RecoveryPolicy | None, Body()] = None,
) -> ChatResponse:
    chat = await session_chat(request, session_id)
    session = str(session_id)
    preview = chat.load(session, "previews", str(preview_id))
    store = request.app.state.store
    run_id, _ = await store.begin_run(session, request.state.user_id)
    request.state.run_id, request.state.session_id = run_id, session
    try:
        result = await chat.confirm(session, str(preview_id))
        job = result["job"]
        monitor = getattr(request.app.state, "task_monitor", None)
        if monitor:
            root_id = preview.get("recovery_root_id", job["id"])
            if policy is not None and not result["reused"]:
                monitor.events.set_policy(root_id, policy.model_dump())
            monitor.events.observe(
                session,
                str(preview_id),
                job,
                report=True,
                diagnose=monitor.events.policy(root_id)["mode"] != "off",
            )
        call_id = str(uuid4())
        execution = ToolExecution(
            call_id=call_id,
            name="calculation_submit_confirmed",
            arguments={"preview_id": str(preview_id)},
            success=True,
            result=result,
        )
        answer = (
            f"{'已找到原任务' if result['reused'] else '已交给 AiiDA'}：#{job['pk']}。"
            f"当前状态：{job['process_state']}。调度器作业号："
            f"{job.get('scheduler_job_id') or '尚未生成'}。可以继续询问状态和结果。"
        )
        if preview.get("workflow") == "bulk_surface":
            answer += " 已确认整个表面流程，AiiDA 将自动推进；完成或失败会更新任务卡并通知。"
        messages = [
            {
                "role": "user",
                "content": f"确认提交预览 {preview_id}"
                f"（{preview['structure']['formula']}，{preview['resources']['queue']}）。",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": execution.name,
                            "arguments": json.dumps(execution.arguments),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)},
            {"role": "assistant", "content": answer},
        ]
        await store.complete_run(session, run_id, messages, answer, [execution], TokenUsage())
        return ChatResponse(
            session_id=session_id,
            run_id=run_id,
            answer=answer,
            tool_calls=[execution],
            usage=TokenUsage(),
        )
    except BaseException:
        await store.fail_run(
            session, run_id, "confirmation_failed", tool_calls=[], usage=TokenUsage()
        )
        raise


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


@router.post("/{session_id}/jobs/{job_id}/recovery")
async def recovery_policy(
    session_id: UUID, job_id: UUID, body: RecoveryPolicy, request: Request
) -> dict:
    chat = await session_chat(request, session_id)
    monitor = getattr(request.app.state, "task_monitor", None)
    if not monitor:
        raise AgentError("monitor_disabled", "后台监控尚未启用。", 503)
    submission = next(
        (
            path
            for path in chat.directory(str(session_id), "submissions").glob("*.json")
            if json.loads(path.read_text())["job"]["id"] == str(job_id)
        ),
        None,
    )
    if submission is None:
        raise AgentError("job_not_found", "当前会话没有该任务。", 404)
    preview = chat.load(str(session_id), "previews", submission.stem)
    root_id = preview.get("recovery_root_id", str(job_id))
    monitor.events.set_policy(root_id, body.model_dump())
    if body.mode != "off":
        from chemistry_agent.automation.events import FAILURE_STATES, task_state

        snapshot = monitor.events.snapshots(str(session_id)).get(str(job_id))
        if snapshot and task_state(snapshot)[0] in FAILURE_STATES:
            monitor.events.enqueue(str(session_id), submission.stem, str(job_id), force=True)
    return {"job_id": str(job_id), "root_job_id": root_id, "policy": body.model_dump()}
