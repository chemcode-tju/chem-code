"""Project/personal research API: no conversation, model or agent is required."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from chemistry_agent.errors import AgentError
from chemistry_agent.memory.models import MemoryForget, MemorySave, MemorySearch
from chemistry_agent.memory.notes import save_note
from chemistry_agent.memory.scope import MemoryScope
from chemistry_agent.memory.service import ResearchMemory, markdown
from chemistry_agent.projects import require_project

router = APIRouter(prefix="/api/research/memories", tags=["research platform"])


async def access(
    request: Request, project_id: UUID | None = None
) -> tuple[ResearchMemory, MemoryScope]:
    if not request.app.state.settings.research_memory_enabled:
        raise AgentError("memory_disabled", "科研记忆已关闭。", 404)
    store = request.app.state.store
    owner = request.state.user_id
    if project_id:
        async with store.lock:
            await require_project(store, str(project_id), owner)
    return ResearchMemory(store), MemoryScope(owner, str(project_id) if project_id else None)


@router.get("")
async def search(
    query: Annotated[MemorySearch, Depends()], resources: tuple = Depends(access)
) -> dict:
    memory, scope = resources
    if query.scope == "session":
        raise AgentError("memory_scope_invalid", "平台检索请选择 project 或 personal 范围。", 422)
    return await memory.search(scope, query)


@router.post("")
async def save(body: MemorySave, resources: tuple = Depends(access)) -> dict:
    memory, scope = resources
    return await save_note(memory, scope, body)


@router.get("/{identifier}")
async def read(identifier: UUID, resources: tuple = Depends(access)) -> dict:
    memory, scope = resources
    return await memory.read(scope, str(identifier))


@router.get("/{identifier}/document.md")
async def export(identifier: UUID, resources: tuple = Depends(access)) -> Response:
    item = await read(identifier, resources)
    return Response(
        markdown(item),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="memory-{identifier}.md"'},
    )


@router.get("/{identifier}/versions")
async def versions(identifier: UUID, resources: tuple = Depends(access)) -> list[dict]:
    memory, scope = resources
    return await memory.versions(scope, str(identifier))


@router.get("/{identifier}/versions/{version}")
async def version(identifier: UUID, version: int, resources: tuple = Depends(access)) -> dict:
    memory, scope = resources
    return await memory.version(scope, str(identifier), version)


@router.post("/{identifier}/forget", status_code=204)
async def forget(identifier: UUID, body: MemoryForget, resources: tuple = Depends(access)) -> None:
    memory, scope = resources
    await memory.forget(scope, str(identifier), body.expected_version)
