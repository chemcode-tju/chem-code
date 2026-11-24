"""Durable guidance addressed to one chat request and its exact active run."""

import json
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from chemistry_agent.automation.routes import local_service
from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import StrictModel

router = APIRouter(prefix="/sessions/{session_id}/chat-requests/{request_id}")


class GuidanceText(StrictModel):
    message: str = Field(min_length=1, max_length=10000)
    run_id: UUID


async def at_boundary(store, request_id: str) -> list[str]:
    """Include previously incorporated guidance when replaying a model checkpoint."""
    async with store.transaction():
        await store.db.execute(
            "UPDATE chat_guidance SET status='included' WHERE request_id=? AND status='pending'",
            (request_id,),
        )
        async with store.db.execute(
            "SELECT message FROM chat_guidance WHERE request_id=? AND status='included' "
            "ORDER BY rowid",
            (request_id,),
        ) as cursor:
            return [row["message"] for row in await cursor.fetchall()]


@router.put("/guidance/{identifier}")
async def put_guidance(
    session_id: UUID, request_id: UUID, identifier: UUID, body: GuidanceText, request: Request
) -> dict:
    state = request.app.state
    session, target, key = str(session_id), str(request_id), str(identifier)
    binding = await state.agent_backends.binding(session)
    native = binding["backend"] == "codex"
    if native or (state.settings.automation_enabled and not state.settings.auth_enabled):
        local_service(request)
    store = state.store
    async with store.transaction():
        row = await store._one(
            "SELECT r.status,r.run_id,c.local FROM chat_requests r LEFT JOIN "
            "chat_checkpoints c ON c.request_id=r.id JOIN sessions s ON s.id=r.session_id "
            "WHERE s.owner_id IS ? AND s.id=? AND r.id=?",
            (request.state.user_id, session, target),
        )
        if not row:
            raise AgentError("request_not_found", "消息不存在。", 404)
        if row["local"]:
            local_service(request)
        existing = await store._one("SELECT * FROM chat_guidance WHERE id=?", (key,))
        if existing and existing["request_id"] == target and existing["message"] == body.message:
            return {"status": existing["status"]}
        if row["status"] != "running" or row["run_id"] != str(body.run_id):
            raise AgentError("turn_finished", "此轮已结束，可排入下一轮。", 409)
        preview = state.chat_queue.live.get(target)
        handler = getattr(preview, "steer", None)
        if binding["backend"] != "builtin" and not (native and handler):
            raise AgentError("steer_unavailable", "此后端当前只能排入下一轮。", 409)
        status = "sending" if native else "pending"
        if existing:
            if existing["request_id"] != target or existing["status"] != "pending":
                raise AgentError("guidance_delivered", "这条引导已不能修改。", 409)
            await store.db.execute(
                "UPDATE chat_guidance SET message=? WHERE id=?", (body.message, key)
            )
        else:
            await store.db.execute(
                "INSERT INTO chat_guidance VALUES(?,?,?,?)", (key, target, body.message, status)
            )
    if native:
        try:
            await handler(key, body.message)
            status = "delivered"
        except Exception:
            # A lost transport acknowledgement must never cause automatic replay.
            status = "unconfirmed"
        async with store.transaction():
            await store.db.execute("UPDATE chat_guidance SET status=? WHERE id=?", (status, key))
    return {"status": status}


@router.delete("/guidance/{identifier}")
async def withdraw(session_id: UUID, request_id: UUID, identifier: UUID, request: Request) -> dict:
    store = request.app.state.store
    async with store.transaction():
        boundary = await store._one(
            "SELECT c.local FROM chat_checkpoints c JOIN chat_requests q ON q.id=c.request_id "
            "JOIN sessions s ON s.id=q.session_id WHERE q.id=? AND s.id=? AND s.owner_id IS ?",
            (str(request_id), str(session_id), request.state.user_id),
        )
        if boundary and boundary["local"]:
            local_service(request)
        cursor = await store.db.execute(
            "UPDATE chat_guidance SET status='withdrawn' WHERE id=? AND request_id=? "
            "AND status='pending' AND EXISTS (SELECT 1 FROM sessions s JOIN chat_requests r "
            "ON r.session_id=s.id WHERE r.id=? AND r.status='running' AND s.id=? AND "
            "s.owner_id IS ?)",
            (
                str(identifier),
                str(request_id),
                str(request_id),
                str(session_id),
                request.state.user_id,
            ),
        )
        if not cursor.rowcount:
            raise AgentError("guidance_delivered", "这条引导已不能修改。", 409)
    return {"status": "withdrawn"}


class QueueEdit(StrictModel):
    previous: str = Field(min_length=1, max_length=20000)
    message: str | None = Field(default=None, min_length=1, max_length=20000)


@router.patch("")
async def edit_queued(
    session_id: UUID, request_id: UUID, body: QueueEdit, request: Request
) -> dict:
    state = request.app.state
    key = str(request_id)
    async with state.store.transaction():
        row = await state.store._one(
            "SELECT q.body,q.status,c.local,c.payload FROM sessions s "
            "JOIN chat_requests q ON q.session_id=s.id JOIN chat_checkpoints c ON "
            "c.request_id=q.id "
            "WHERE s.owner_id IS ? AND s.id=? AND q.id=?",
            (request.state.user_id, str(session_id), key),
        )
        if not row:
            raise AgentError("request_not_found", "消息不存在。", 404)
        if row["local"]:
            local_service(request)
        interactive = await state.store._one(
            "SELECT 1 FROM messages WHERE session_id=? AND json_extract(payload,'$.role')='tool' "
            "AND instr(payload,?)>0 LIMIT 1",
            (str(session_id), key),
        )
        payload = json.loads(row["body"])
        if (
            interactive
            or key in state.chat_queue.claimed
            or row["status"] != "queued"
            or row["payload"]
            or payload["message"] != body.previous
        ):
            raise AgentError("queue_changed", "消息已开始处理或已更改。", 409)
        status = "withdrawn" if body.message is None else "queued"
        if body.message is not None:
            payload["message"] = body.message
        await state.store.db.execute(
            "UPDATE chat_requests SET body=?,status=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), status, key),
        )
    return {"status": status}
