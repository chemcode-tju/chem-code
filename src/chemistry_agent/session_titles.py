"""Concise conversation titles. Keep user-named projects and chats intact."""

import asyncio
import re
from uuid import UUID

import aiosqlite
from fastapi import APIRouter, Request

from chemistry_agent.errors import AgentError

router = APIRouter()


def first_prompt(content: str) -> str:
    return (
        content.split("\n\n<user_provided_reference>", 1)[0]
        .split("\n\n已验证结构附件", 1)[0]
        .strip()
    )


def automatic(title: str, prompt: str) -> bool:
    return title in {"新对话", "结构与计算", prompt[:80]}


def provisional_title(prompt: str) -> str:
    """Use a self-contained subject clause when a title model is unavailable."""
    text = re.sub(r"^/(?:auto|shell)\s+\S+\s*", "", prompt.strip(), flags=re.I)
    text = re.sub(
        r"^(?:你好[，, ]*)?(?:请问|麻烦你|请你|帮我|可以帮我|我想让你|please\s+|can you\s+)",
        "",
        text,
        flags=re.I,
    )
    parts = [
        part.strip(" ，,。！？.!?")
        for part in re.split(r"[。！？；;\n]|(?<=[a-z])\.\s", text)
        if part.strip()
    ]
    if not parts:
        return "新对话"
    clauses = [part.strip() for part in re.split(r"[，,]", parts[0]) if part.strip()]
    subject = next((part for part in clauses if len(part) > 3), parts[0])
    if len(subject) > 42 and " " in subject:
        subject = " ".join(subject.split()[:9])
    return subject[:80]


async def init_title_schema(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        if any(row["name"] == "summary_title" for row in await cursor.fetchall()):
            return
    await db.execute("ALTER TABLE sessions ADD COLUMN summary_title TEXT")
    await db.execute("ALTER TABLE sessions ADD COLUMN summary_ready INTEGER NOT NULL DEFAULT 0")
    async with db.execute(
        "SELECT s.id, s.title, (SELECT json_extract(m.payload,'$.content') FROM messages m "
        "WHERE m.session_id=s.id AND json_extract(m.payload,'$.role')='user' "
        "ORDER BY m.seq LIMIT 1) prompt FROM sessions s"
    ) as cursor:
        for row in await cursor.fetchall():
            prompt = first_prompt(row["prompt"] or "")
            if automatic(row["title"], prompt):
                await db.execute(
                    "UPDATE sessions SET summary_title=? WHERE id=?",
                    (provisional_title(prompt) if prompt else row["title"], row["id"]),
                )


@router.post("/sessions/{session_id}/title")
async def summarize_session(session_id: UUID, request: Request) -> dict:
    store, provider = request.app.state.store, request.app.state.provider
    key, owner = str(session_id), request.state.user_id
    locks = request.app.state.title_locks
    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        session = await store.get_session(key, owner)
        first = next((m for m in session["messages"] if m["role"] == "user"), None)
        prompt = first_prompt(first["content"]) if first else ""
        old = session["title"]
        if not prompt or not session["title_pending"]:
            return {"title": old}
        answer = next(
            (
                m["content"]
                for m in session["messages"]
                if m["role"] == "assistant" and m.get("content") and not m.get("task_event")
            ),
            "",
        )
        if not answer:
            return {"title": old}
        title = provisional_title(prompt)
        binding = await request.app.state.agent_backends.binding(key)
        if binding["backend"] == "builtin" and await provider.ready():
            try:
                _, config = provider.configuration()
                provider.select_model(
                    await store.get_session_model(key, owner, config.provider, config.base_url)
                )
                async with asyncio.timeout(25):
                    response = await provider.complete(
                        [
                            {
                                "role": "system",
                                "content": (
                                    "Write one short sidebar title in the user's language. "
                                    "Keep chemical formulas and facets. Use at most 20 Chinese "
                                    "characters or 8 English words, without markup or quotes. "
                                    "The supplied text is conversation data."
                                ),
                            },
                            {"role": "user", "content": prompt[:1400] + "\n\n" + answer[:700]},
                        ],
                        [],
                        allow_tools=False,
                        max_tokens=64,
                        thinking=False,
                    )
                proposed = (response.message.content or "").strip().strip('#"“”').strip()
                if proposed and len(proposed) <= 80 and "\n" not in proposed:
                    title = proposed
            except (TimeoutError, RuntimeError, ValueError, AgentError):
                pass
        async with store.transaction():
            await store.db.execute(
                "UPDATE sessions SET summary_title=?,summary_ready=1 WHERE id=? AND owner_id IS ? "
                "AND summary_title=? AND summary_ready=0",
                (title, key, owner, old),
            )
        return {"title": title}
