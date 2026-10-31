"""Browser confirmation is authoritative; no LLM tool can submit a job."""

import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response

from chemistry_agent.automation.chat import CalculationChat, ImportInput
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
    return {
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
async def confirm(session_id: UUID, preview_id: UUID, request: Request) -> ChatResponse:
    chat = await session_chat(request, session_id)
    session = str(session_id)
    preview = chat.load(session, "previews", str(preview_id))
    store = request.app.state.store
    run_id, _ = await store.begin_run(session, request.state.user_id)
    request.state.run_id, request.state.session_id = run_id, session
    try:
        result = await chat.confirm(session, str(preview_id))
        job = result["job"]
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
