"""Durable chat receipts; inference continues independently of the browser connection."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite
from starlette.requests import Request

from chemistry_agent.automation.routes import local_service
from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ModelScope, ProviderRouter, current_model_scope
from chemistry_agent.runtime.checkpoint import UNCERTAIN, Checkpoint, current_checkpoint
from chemistry_agent.runtime.live import LiveAnswer
from chemistry_agent.schemas import ChatRequest
from chemistry_agent.store import Store, utc_now


class ChatQueue:
    def __init__(
        self, store: Store, execute: Callable[..., Awaitable[Any]], provider: ProviderRouter
    ) -> None:
        self.store = store
        self.execute = execute
        self.provider = provider
        self.live: dict[str, LiveAnswer] = {}
        self.tasks: set[asyncio.Task] = set()
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.account_locks: dict[str | None, asyncio.Lock] = {}

    async def enqueue(
        self, identifier: str, body: ChatRequest, request: Request, retry: bool
    ) -> dict:
        session = str(body.session_id)
        encoded = json.dumps(body.model_dump(mode="json", exclude_unset=True), sort_keys=True)
        user_id, config = self.provider.configuration()
        try:
            local_service(request)
            local = True
        except AgentError:
            local = False
        async with self.store.transaction():
            if not await self.store._one(
                "SELECT 1 FROM sessions WHERE id=? AND owner_id IS ?",
                (session, request.state.user_id),
            ):
                raise AgentError("session_not_found", "会话不存在。", 404)
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
            await self.store.db.execute(
                "INSERT OR IGNORE INTO chat_checkpoints(request_id,local,provider,destination) "
                "VALUES(?,?,?,?)",
                (identifier, local, config.provider, config.base_url),
            )
        # Copy request-local state: background work must not write to the HTTP request.
        detached = Request({**request.scope, "state": dict(request.scope.get("state", {}))})
        detached.state.chat_request_id = identifier
        self.schedule(identifier, body, detached, ModelScope(user_id, config.model_copy(deep=True)))
        return {"id": identifier, "session_id": session, "status": "queued"}

    def schedule(
        self, identifier: str, body: ChatRequest, request: Request, scope: ModelScope
    ) -> None:
        self.live[identifier] = LiveAnswer()
        token = current_model_scope.set(scope)
        try:
            task = asyncio.create_task(self.run(identifier, body, request))
        finally:
            current_model_scope.reset(token)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def recover(self, app) -> None:
        """Replay persisted receipts in FIFO order using their original security boundary."""
        async with (
            self.store.lock,
            self.store.db.execute(
                "SELECT r.*,s.owner_id,c.local,c.provider,c.destination FROM chat_requests r "
                "JOIN sessions s ON s.id=r.session_id LEFT JOIN chat_checkpoints c "
                "ON c.request_id=r.id "
                "WHERE r.status='queued' ORDER BY r.rowid"
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        for row in rows:
            owner = row["owner_id"]
            if app.state.settings.auth_enabled != (owner is not None):
                await self.fail(row["id"], "工作区登录模式已改变，请核对原消息。")
                continue
            config = self.provider.store.load(owner)
            if row["provider"] and (row["provider"], row["destination"]) != (
                config.provider,
                config.base_url,
            ):
                await self.fail(row["id"], "模型服务地址已改变；请确认之后再重试。")
                continue
            peer = "127.0.0.1" if row["local"] else "unknown"
            request = Request(
                {
                    "type": "http",
                    "app": app,
                    "scheme": "http",
                    "path": "/chat",
                    "headers": [],
                    "server": (peer, 80),
                    "client": (peer, 0),
                    "query_string": b"",
                    "state": {"user_id": owner, "chat_request_id": row["id"]},
                }
            )
            body = ChatRequest.model_validate_json(row["body"])
            if row["provider"] is None:
                async with self.store.transaction():
                    await self.store.db.execute(
                        "INSERT INTO chat_checkpoints(request_id,local,provider,destination) "
                        "VALUES(?,?,?,?)",
                        (row["id"], False, config.provider, config.base_url),
                    )
            self.schedule(row["id"], body, request, ModelScope(owner, config))

    @staticmethod
    def public(row: aiosqlite.Row) -> dict:
        return {**dict(row), "body": json.loads(row["body"]) if row["body"] else None}

    async def list(self, session: str, owner: str | None, *, compact: bool = False) -> dict:
        async with self.store.lock:
            state = await self.store._one(
                "SELECT active_run_id,updated_at FROM sessions WHERE id=? AND owner_id IS ?",
                (session, owner),
            )
            if state is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            async with self.store.db.execute(
                "SELECT id,session_id,created_at,run_id,status,error,"
                "CASE WHEN status='completed' AND ? THEN NULL ELSE body END AS body "
                "FROM chat_requests WHERE session_id=? AND (status IN ('queued','running') OR "
                "rowid IN (SELECT rowid FROM chat_requests WHERE session_id=? "
                "ORDER BY rowid DESC LIMIT 100)) "
                "ORDER BY rowid DESC",
                (compact, session, session),
            ) as cursor:
                rows = await cursor.fetchall()
        return {**dict(state), "requests": [self.public(row) for row in reversed(rows)]}

    async def run(self, identifier: str, body: ChatRequest, request: Request) -> None:
        session = str(body.session_id)
        lock = self.session_locks.setdefault(session, asyncio.Lock())
        checkpoint_token = current_checkpoint.set(Checkpoint(self.store, identifier))
        request.state.live_answer = self.live[identifier]
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
            # Leave the receipt for startup recovery.
            raise
        except AgentError as exc:
            await self.fail(identifier, exc.message)
        except Exception:
            await self.fail(identifier, "本轮处理失败，消息已保存，可点击重试。")

        finally:
            current_checkpoint.reset(checkpoint_token)
            self.live.pop(identifier).close()

    async def fail(self, identifier: str, message: str) -> None:
        async with self.store.transaction():
            checkpoint = await self.store._one(
                "SELECT phase FROM chat_checkpoints WHERE request_id=?", (identifier,)
            )
            uncertain = checkpoint and checkpoint["phase"] == "tools"
            await self.store.db.execute(
                "UPDATE chat_requests SET status=?,error=? WHERE id=? AND status!='completed'",
                (
                    "uncertain" if uncertain else "failed",
                    UNCERTAIN if uncertain else message,
                    identifier,
                ),
            )

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
