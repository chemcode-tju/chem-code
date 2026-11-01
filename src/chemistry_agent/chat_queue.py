"""Durable chat receipts; inference continues independently of the browser connection."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite
from starlette.requests import Request

from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ModelScope, ProviderRouter, current_model_scope
from chemistry_agent.schemas import ChatRequest
from chemistry_agent.store import Store, utc_now


class ChatQueue:
    def __init__(
        self, store: Store, execute: Callable[..., Awaitable[Any]], provider: ProviderRouter
    ) -> None:
        self.store = store
        self.execute = execute
        self.provider = provider
        self.tasks: set[asyncio.Task] = set()
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.account_locks: dict[str | None, asyncio.Lock] = {}

    async def enqueue(
        self, identifier: str, body: ChatRequest, request: Request, retry: bool
    ) -> dict:
        session = str(body.session_id)
        await self.store.get_session(session, request.state.user_id)
        encoded = json.dumps(body.model_dump(mode="json", exclude_unset=True), sort_keys=True)
        async with self.store.transaction():
            existing = await self.store._one(
                "SELECT * FROM chat_requests WHERE id=?", (identifier,)
            )
            if existing:
                if existing["session_id"] != session or existing["body"] != encoded:
                    raise AgentError("request_id_conflict", "此消息编号已用于另一条消息。", 409)
                if not retry or existing["status"] != "failed":
                    return self.public(existing)
                await self.store.db.execute(
                    "UPDATE chat_requests SET status='queued',run_id=NULL,error=NULL WHERE id=?",
                    (identifier,),
                )
            else:
                await self.store.db.execute(
                    "INSERT INTO chat_requests(id,session_id,body,status,created_at) "
                    "VALUES (?,?,?,'queued',?)",
                    (identifier, session, encoded, utc_now()),
                )
        # Copy request-local state: background work must not write to the HTTP request.
        detached = Request({**request.scope, "state": dict(request.scope.get("state", {}))})
        detached.state.chat_request_id = identifier
        user_id, config = self.provider.configuration()
        token = current_model_scope.set(ModelScope(user_id, config.model_copy(deep=True)))
        try:
            task = asyncio.create_task(self.run(identifier, body, detached))
        finally:
            current_model_scope.reset(token)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {"id": identifier, "session_id": session, "status": "queued"}

    @staticmethod
    def public(row: aiosqlite.Row) -> dict:
        return {**dict(row), "body": json.loads(row["body"])}

    async def list(self, session: str, owner: str | None) -> dict:
        async with self.store.lock:
            state = await self.store._one(
                "SELECT active_run_id,updated_at FROM sessions WHERE id=? AND owner_id IS ?",
                (session, owner),
            )
            if state is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            async with self.store.db.execute(
                "SELECT * FROM chat_requests WHERE session_id=? ORDER BY rowid DESC LIMIT 100",
                (session,),
            ) as cursor:
                rows = await cursor.fetchall()
        return {**dict(state), "requests": [self.public(row) for row in reversed(rows)]}

    async def run(self, identifier: str, body: ChatRequest, request: Request) -> None:
        session = str(body.session_id)
        lock = self.session_locks.setdefault(session, asyncio.Lock())
        try:
            async with lock:
                user_id, config = self.provider.configuration()
                # ChatGPT inference is serialized per account. Queue complete turns before
                # begin_run/its timeout, rather than interleaving five turns behind that lock.
                slot = (
                    self.account_locks.setdefault(user_id, asyncio.Lock())
                    if config.provider == "chatgpt"
                    else asyncio.Lock()
                )
                async with slot:
                    while True:
                        try:
                            await self.execute(body, request)
                            return
                        except AgentError as exc:
                            if exc.code != "session_busy":
                                raise
                            # Legacy chat/submission can own this session temporarily.
                            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            await self.fail(identifier, "服务已重启，消息已保存，可点击重试。")
            raise
        except AgentError as exc:
            await self.fail(identifier, exc.message)
        except Exception:
            await self.fail(identifier, "本轮处理失败，消息已保存，可点击重试。")

    async def fail(self, identifier: str, message: str) -> None:
        async with self.store.transaction():
            await self.store.db.execute(
                "UPDATE chat_requests SET status='failed',error=? "
                "WHERE id=? AND status!='completed'",
                (message, identifier),
            )

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
