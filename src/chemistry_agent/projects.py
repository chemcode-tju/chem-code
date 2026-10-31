"""Project folders, kept separate from chat execution and provider configuration."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import aiosqlite
from fastapi import APIRouter, Request
from pydantic import Field, field_validator

from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import StrictModel

if TYPE_CHECKING:
    from chemistry_agent.store import Store


router = APIRouter(prefix="/api/projects", tags=["projects"])


async def init_project_schema(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id), name TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner_id);
        CREATE TABLE IF NOT EXISTS project_sessions (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS project_members ON project_sessions(project_id);
    """)


class ProjectName(StrictModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目名称不能为空。")
        return value.strip()


class ProjectAssignment(StrictModel):
    project_id: UUID | None = None


async def require_project(store: "Store", project_id: str, owner_id: str | None) -> None:
    row = await store._one(
        "SELECT id FROM projects WHERE id=? AND owner_id IS ?", (project_id, owner_id)
    )
    if row is None:
        raise AgentError("project_not_found", "项目不存在。", 404)


@router.get("")
async def list_projects(request: Request) -> list[dict]:
    store = request.app.state.store
    async with store.lock:
        async with store.db.execute(
            "SELECT id, name FROM projects WHERE owner_id IS ? ORDER BY rowid",
            (request.state.user_id,),
        ) as cursor:
            projects = [dict(row) for row in await cursor.fetchall()]
        async with store.db.execute(
            "SELECT s.id, s.title, ps.project_id FROM sessions s "
            "JOIN project_sessions ps ON ps.session_id=s.id "
            "JOIN projects p ON p.id=ps.project_id "
            "WHERE s.owner_id IS ? AND p.owner_id IS ? ORDER BY s.updated_at DESC",
            (request.state.user_id, request.state.user_id),
        ) as cursor:
            sessions = [dict(row) for row in await cursor.fetchall()]
    return [
        {**project, "sessions": [s for s in sessions if s["project_id"] == project["id"]]}
        for project in projects
    ]


@router.post("", status_code=201)
async def create_project(body: ProjectName, request: Request) -> dict:
    store = request.app.state.store
    project_id = str(uuid4())
    async with store.transaction():
        await store.db.execute(
            "INSERT INTO projects(id, owner_id, name) VALUES(?,?,?)",
            (project_id, request.state.user_id, body.name),
        )
    return {"id": project_id, "name": body.name, "sessions": []}


@router.patch("/{project_id}")
async def rename_project(project_id: UUID, body: ProjectName, request: Request) -> dict:
    store = request.app.state.store
    async with store.transaction():
        await require_project(store, str(project_id), request.state.user_id)
        await store.db.execute(
            "UPDATE projects SET name=? WHERE id=?", (body.name, str(project_id))
        )
    return {"id": str(project_id), "name": body.name}


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: UUID, request: Request) -> None:
    store = request.app.state.store
    async with store.transaction():
        await require_project(store, str(project_id), request.state.user_id)
        # Removing a folder never deletes its conversations.
        await store.db.execute("DELETE FROM projects WHERE id=?", (str(project_id),))


@router.put("/sessions/{session_id}", status_code=204)
async def assign_session(session_id: UUID, body: ProjectAssignment, request: Request) -> None:
    store = request.app.state.store
    async with store.transaction():
        row = await store._one(
            "SELECT id FROM sessions WHERE id=? AND owner_id IS ?",
            (str(session_id), request.state.user_id),
        )
        if row is None:
            raise AgentError("session_not_found", "会话不存在。", 404)
        if body.project_id:
            await require_project(store, str(body.project_id), request.state.user_id)
        await store.db.execute(
            "DELETE FROM project_sessions WHERE session_id=?", (str(session_id),)
        )
        if body.project_id:
            await store.db.execute(
                "INSERT INTO project_sessions(session_id, project_id) VALUES(?,?)",
                (str(session_id), str(body.project_id)),
            )
