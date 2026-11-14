"""Local-only Agent configuration. Credentials remain in the Agent's own store."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel

from chemistry_agent.automation.routes import local_service
from chemistry_agent.backends.service import BackendSettings, SessionSelection

router = APIRouter(prefix="/api/agents", tags=["agent backends"])


@router.get("/settings")
async def settings(request: Request) -> dict:
    local_service(request)
    return await request.app.state.agent_backends.public()


@router.put("/settings")
async def save(body: BackendSettings, request: Request) -> dict:
    local_service(request)
    return await request.app.state.agent_backends.save_settings(body)


@router.post("/codex/probe")
async def probe(body: BackendSettings, request: Request) -> dict:
    local_service(request)
    return await request.app.state.agent_backends.codex.probe(body)


@router.post("/antigravity/probe")
async def probe_antigravity(body: BackendSettings, request: Request) -> dict:
    local_service(request)
    return await request.app.state.agent_backends.antigravity.probe(body, refresh=True)


@router.get("/sessions/{session_id}")
async def binding(session_id: UUID, request: Request) -> dict:
    local_service(request)
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    item = await request.app.state.agent_backends.binding(str(session_id))
    return {
        k: item[k] for k in ("backend", "external_thread_id", "turn_state")
    } | await request.app.state.agent_backends.selection(
        str(session_id), request.app.state.provider
    )


@router.get("/selection")
async def defaults(request: Request) -> dict:
    local_service(request)
    return await request.app.state.agent_backends.selection(None, request.app.state.provider)


@router.put("/sessions/{session_id}")
async def select(session_id: UUID, body: SessionSelection, request: Request) -> dict:
    local_service(request)
    return await request.app.state.agent_backends.select(
        str(session_id), body, request.app.state.provider
    )


@router.get("/catalog/{backend}")
async def catalog(backend: str, request: Request) -> dict:
    local_service(request)
    backends = request.app.state.agent_backends
    if backend == "codex":
        return await backends.codex.probe(await backends.settings())
    if backend == "antigravity":
        return await backends.antigravity.probe(await backends.settings())
    if backend != "builtin":
        from chemistry_agent.errors import AgentError

        raise AgentError("agent_backend_unavailable", "此 Agent 适配器尚未安装。", 404)
    provider = request.app.state.provider
    return await provider.list_models() | {"configured": await provider.ready()}


class ApprovalAnswer(BaseModel):
    approved: bool


@router.get("/sessions/{session_id}/approvals")
async def approvals(session_id: UUID, request: Request) -> dict:
    local_service(request)
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    return {"items": await request.app.state.agent_backends.approvals.list_pending(str(session_id))}


@router.post("/sessions/{session_id}/approvals/{identifier}")
async def approve(
    session_id: UUID, identifier: UUID, body: ApprovalAnswer, request: Request
) -> dict:
    local_service(request)
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    await request.app.state.agent_backends.approvals.resolve(
        str(session_id), str(identifier), body.approved
    )
    return {"ok": True}
