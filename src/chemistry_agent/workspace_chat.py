"""Session-bound side discussions and cross-session items needing user attention."""

import asyncio
import json
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid5

from fastapi import APIRouter, Request
from pydantic import Field

from chemistry_agent.automation.routes import local_service
from chemistry_agent.backends.service import BackendSettings
from chemistry_agent.backends.side_chat import SIDE_PROMPT, native_side_answer
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.attention import pending_actions
from chemistry_agent.schemas import StrictModel

router = APIRouter()


class SideQuestion(StrictModel):
    id: UUID
    question: str = Field(min_length=1, max_length=4000)


@router.get("/sessions/{session_id}/side-chat")
async def side_history(session_id: UUID, request: Request) -> dict:
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    store = request.app.state.store
    async with (
        store.lock,
        store.db.execute(
            "SELECT id,question,answer,error FROM side_answers WHERE session_id=? ORDER BY rowid",
            (str(session_id),),
        ) as cursor,
    ):
        return {"items": [dict(row) for row in await cursor.fetchall()]}


@router.post("/sessions/{session_id}/side-chat")
async def ask_side(session_id: UUID, body: SideQuestion, request: Request) -> dict:
    session, key = str(session_id), str(body.id)
    await request.app.state.store.require_session(session, request.state.user_id)
    state = request.app.state
    store = state.store
    binding = await state.agent_backends.binding(session)
    if binding["backend"] != "builtin":
        local_service(request)
    elif not await state.provider.ready():
        raise AgentError("side_model_required", "请先在设置中连接模型。", 409)
    async with store.transaction():
        existing = await store._one("SELECT * FROM side_answers WHERE id=?", (key,))
        if existing:
            if existing["session_id"] != session or existing["question"] != body.question:
                raise AgentError("request_id_conflict", "消息编号已被使用。", 409)
            return {"id": key, "answer": existing["answer"], "error": existing["error"]}
        if await store._one(
            "SELECT 1 FROM side_answers WHERE session_id=? AND answer IS NULL AND error IS NULL",
            (session,),
        ):
            raise AgentError("side_busy", "上一条旁问正在回答。", 409)
        async with store.db.execute(
            "SELECT json_extract(payload,'$.role') role, "
            "substr(json_extract(payload,'$.content'),1,3000) content, "
            "substr(json_extract(payload,'$.guidance'),1,2000) guidance "
            "FROM messages WHERE session_id=? AND json_extract(payload,'$.role') IN "
            "('user','assistant','tool') "
            "AND json_extract(payload,'$.content') IS NOT NULL ORDER BY seq DESC LIMIT 12",
            (session,),
        ) as cursor:
            main = [dict(row) for row in reversed(await cursor.fetchall())]
        active = await store._one(
            "SELECT id, json_extract(body,'$.message') message FROM chat_requests "
            "WHERE session_id=? AND status='running'",
            (session,),
        )
        if active:
            main.append({"role": "user", "content": active["message"][:3000]})
            async with store.db.execute(
                "SELECT message FROM chat_guidance WHERE request_id=? AND status IN "
                "('included','delivered') ORDER BY rowid DESC LIMIT 4",
                (active["id"],),
            ) as cursor:
                main.extend(
                    {"role": "user", "content": row["message"][:3000]}
                    for row in reversed(await cursor.fetchall())
                )
            live = state.chat_queue.live.get(active["id"])
            if live and live.text:
                main.append({"role": "assistant", "content": live.text[-3000:]})
        async with store.db.execute(
            "SELECT question,answer FROM side_answers WHERE session_id=? "
            "AND answer IS NOT NULL ORDER BY rowid DESC LIMIT 8",
            (session,),
        ) as cursor:
            history = list(reversed(await cursor.fetchall()))
        await store.db.execute(
            "INSERT INTO side_answers(id,session_id,question) VALUES(?,?,?)",
            (key, session, body.question),
        )
    try:
        counter = state.context_manager.counter
        budget = state.settings.context_input_budget
        question = {"role": "user", "content": body.question}
        system = {"role": "system", "content": SIDE_PROMPT}
        messages = [system, question]
        for previous in history:
            messages[-1:-1] = [
                {"role": "user", "content": previous["question"]},
                {"role": "assistant", "content": previous["answer"][:4000]},
            ]
        # Prefer recent parent evidence and the newest side exchange in a small window.
        while len(messages) > 2 and counter.request_tokens(messages, []) > budget // 2:
            del messages[1:3]
        while main:
            excerpt = {
                "role": "user",
                "content": "主对话摘录\n" + json.dumps(main, ensure_ascii=False),
            }
            if counter.request_tokens([system, excerpt, *messages[1:]], []) <= budget:
                messages.insert(1, excerpt)
                break
            main.pop(0)
        if counter.request_tokens(messages, []) > budget:
            raise AgentError("side_context_limit", "旁问超过模型输入长度，请缩短问题。", 422)
        async with asyncio.timeout(state.settings.run_timeout_seconds):
            if binding["backend"] != "builtin":
                config = BackendSettings.model_validate_json(binding["config"])
                answer = await native_side_answer(
                    state.agent_backends,
                    binding["backend"],
                    config,
                    str(uuid5(session_id, "side")),
                    json.dumps(messages[1:], ensure_ascii=False),
                )
            else:
                provider = state.provider
                _, config = provider.configuration()
                provider.select_model(
                    await store.get_session_model(
                        session, request.state.user_id, config.provider, config.base_url
                    )
                )
                response = await provider.complete(messages, [], allow_tools=False)
                answer = response.message.content
                if not answer:
                    raise AgentError("side_empty", "侧边对话未返回正文。", 502)
        async with store.transaction():
            await store.db.execute("UPDATE side_answers SET answer=? WHERE id=?", (answer, key))
        return {"id": key, "answer": answer}
    except (Exception, asyncio.CancelledError) as exc:
        error = exc.message if isinstance(exc, AgentError) else "侧边对话未完成，请重新提问。"
        async with store.transaction():
            await store.db.execute("UPDATE side_answers SET error=? WHERE id=?", (error, key))
        if isinstance(exc, (asyncio.CancelledError, AgentError)):
            raise
        raise AgentError("side_failed", error, 502) from exc


@lru_cache(maxsize=256)
def active_plan(path: Path, modified_ns: int) -> dict | None:
    """Parse changed platform snapshots once; terminal plans need no attention query."""
    plan = json.loads(path.read_text())
    return None if plan["status"] in {"draft", "stopping", "stopped", "completed"} else plan


def session_plans(root: Path, sessions: dict[str, str]) -> list[tuple[str, dict]]:
    plans = []
    for session in sessions:
        for path in (root / session / "plans").glob("*.json"):
            try:
                plan = active_plan(path, path.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
            if plan:
                plans.append((session, plan))
    return plans


@router.get("/api/chat/attention")
async def attention(request: Request) -> dict:
    state, owner = request.app.state, request.state.user_id
    store = state.store
    async with (
        store.lock,
        store.db.execute(
            "SELECT s.id,COALESCE(s.summary_title,s.title) title,(SELECT q.status FROM "
            "chat_requests q "
            "WHERE q.session_id=s.id ORDER BY q.rowid DESC LIMIT 1) status FROM sessions s "
            "WHERE owner_id IS ? AND NOT EXISTS(SELECT 1 FROM task_agent_sessions t "
            "WHERE t.session_id=s.id) ORDER BY s.updated_at DESC",
            (owner,),
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    sessions = {row["id"]: row["title"] for row in rows}
    statuses = {row["id"]: row["status"] for row in rows if row["status"]}
    items = []
    async with (
        store.lock,
        store.db.execute(
            "SELECT r.session_id,r.id,r.status FROM chat_requests r JOIN sessions s ON "
            "s.id=r.session_id "
            "WHERE s.owner_id IS ? AND r.status='uncertain' AND r.rowid=(SELECT MAX(q.rowid) "
            "FROM chat_requests q WHERE q.session_id=s.id) UNION ALL "
            "SELECT a.session_id,a.id,'approval' FROM agent_approvals a JOIN sessions s ON "
            "s.id=a.session_id "
            "WHERE s.owner_id IS ? AND a.state='pending' UNION ALL "
            "SELECT s.id,json_extract(m.payload,'$.content'),'question' FROM sessions s "
            "JOIN messages m "
            "ON m.session_id=s.id WHERE s.owner_id IS ? AND "
            "json_extract(m.payload,'$.role')='tool' "
            "AND instr(m.payload,'awaiting_user')>0 AND m.seq>(SELECT MAX(seq) FROM messages u "
            "WHERE u.session_id=s.id AND json_extract(u.payload,'$.role')='user') "
            "AND NOT EXISTS(SELECT 1 FROM chat_requests q WHERE q.session_id=s.id AND "
            "q.status IN ('queued','running'))",
            (owner, owner, owner),
        ) as cursor,
    ):
        for session, key, kind in await cursor.fetchall():
            if session not in sessions:
                continue
            if kind == "question":
                data = json.loads(key)
                if not isinstance(data, dict) or not data.get("question_id"):
                    continue
                key = data["question_id"]
            items.append({"session": session, "title": sessions[session], "kind": kind, "id": key})
    if state.settings.automation_enabled and not state.settings.auth_enabled:
        try:
            local_service(request)
        except AgentError:
            return {"items": items, "statuses": statuses}
        chat = state.calculation_chat
        for session, plan in await asyncio.to_thread(session_plans, chat.root, sessions):
            for action in await pending_actions(chat.plans, session, plan):
                items.append(
                    {
                        "session": session,
                        **action,
                        "detail": action.get("job_title") or action["title"],
                        "title": sessions[session],
                        "plan": plan["id"],
                        "id": action.get("approval_id") or action.get("job_id"),
                    }
                )
    return {"items": items, "statuses": statuses}
