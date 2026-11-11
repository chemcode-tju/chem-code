"""Authenticated memory management and lossless Markdown export."""

import json
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response

from chemistry_agent.errors import AgentError
from chemistry_agent.memory.models import (
    MemoryCompare,
    MemoryForget,
    MemorySave,
    MemorySearch,
)
from chemistry_agent.memory.service import ResearchMemory, markdown
from chemistry_agent.memory.tools import save_note

router = APIRouter(tags=["research memory"])


async def authorized(request: Request, session: UUID) -> ResearchMemory:
    if not request.app.state.settings.research_memory_enabled:
        raise AgentError("memory_disabled", "科研记忆已关闭。", 404)
    store = request.app.state.store
    async with store.lock:
        row = await store._one(
            "SELECT id FROM sessions WHERE id=? AND owner_id IS ?",
            (str(session), request.state.user_id),
        )
    if row is None:
        raise AgentError("session_not_found", "会话不存在。", 404)
    return ResearchMemory(store)


@router.get("/memory", include_in_schema=False)
async def memory_page() -> FileResponse:
    from pathlib import Path

    return FileResponse(Path(__file__).parents[1] / "web" / "memory.html")


@router.get("/api/sessions/{session}/memories")
async def search_memories(
    session: UUID,
    request: Request,
    query: str = Query(default="", max_length=2000),
    scope: str = "project",
    kind: str | None = None,
    limit: int = Query(default=20, ge=1, le=20),
    offset: int = Query(default=0, ge=0),
) -> dict:
    from pydantic import ValidationError

    memory = await authorized(request, session)
    try:
        args = MemorySearch(query=query, scope=scope, kind=kind, limit=limit, offset=offset)
    except ValidationError as exc:
        raise AgentError("invalid_memory_query", "检索范围或类型无效。", 422) from exc
    return await memory.search(str(session), args)


@router.post("/api/sessions/{session}/memories")
async def write_memory(session: UUID, body: MemorySave, request: Request) -> dict:
    return await save_note(await authorized(request, session), str(session), body)


@router.get("/api/sessions/{session}/memories/{identifier}")
async def read_memory(session: UUID, identifier: UUID, request: Request) -> dict:
    return await (await authorized(request, session)).read(str(session), str(identifier))


@router.get("/api/sessions/{session}/memories/{identifier}/document.md")
async def export_memory(session: UUID, identifier: UUID, request: Request) -> Response:
    item = await (await authorized(request, session)).read(str(session), str(identifier))
    return Response(
        markdown(item),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="memory-{identifier}.md"',
        },
    )


@router.get("/api/sessions/{session}/memories/{identifier}/versions")
async def memory_versions(session: UUID, identifier: UUID, request: Request) -> list[dict]:
    memory = await authorized(request, session)
    await memory.read(str(session), str(identifier))
    async with memory.store.lock:
        async with memory.store.db.execute(
            "SELECT version,created_at FROM research_memory_versions "
            "WHERE memory_id=? ORDER BY version DESC",
            (str(identifier),),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


@router.get("/api/sessions/{session}/memories/{identifier}/versions/{version}")
async def memory_version(session: UUID, identifier: UUID, version: int, request: Request) -> dict:
    memory = await authorized(request, session)
    await memory.read(str(session), str(identifier))
    async with memory.store.lock:
        row = await memory.store._one(
            "SELECT document FROM research_memory_versions WHERE memory_id=? AND version=?",
            (str(identifier), version),
        )
    if row is None:
        raise AgentError("memory_not_found", "版本不存在。", 404)
    return json.loads(row["document"]) | {"id": str(identifier), "version": version}


@router.post("/api/sessions/{session}/memories/{identifier}/forget", status_code=204)
async def forget_memory(
    session: UUID, identifier: UUID, body: MemoryForget, request: Request
) -> None:
    memory = await authorized(request, session)
    await memory.forget(str(session), str(identifier), body.expected_version)


@router.post("/api/sessions/{session}/memory-compare")
async def compare_memory(session: UUID, body: MemoryCompare, request: Request) -> dict:
    memory = await authorized(request, session)
    return await memory.compare(str(session), str(body.memory_id), body.parameters)
