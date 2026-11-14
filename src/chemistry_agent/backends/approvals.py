"""Browser approvals belong to a specific run and expire on interruption/restart."""

import asyncio
import json
from contextvars import ContextVar
from uuid import uuid4

from chemistry_agent.errors import AgentError

current_tool_approval: ContextVar = ContextVar("tool_approval", default=None)


class ApprovalBroker:
    def __init__(self, store) -> None:
        self.store = store
        self.pending: dict[str, asyncio.Future] = {}

    async def initialize(self) -> None:
        await self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS agent_approvals (id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL, run_id TEXT NOT NULL, "
            "payload TEXT NOT NULL, state TEXT NOT NULL)"
        )
        await self.store.db.execute(
            "UPDATE agent_approvals SET state='expired' WHERE state='pending'"
        )

    async def ask(self, session: str, run: str, payload: dict) -> bool:
        identifier = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            async with self.store.transaction():
                await self.store.db.execute(
                    "INSERT INTO agent_approvals VALUES(?,?,?,?, 'pending')",
                    (identifier, session, run, json.dumps(payload, ensure_ascii=False)),
                )
            return await future
        finally:
            self.pending.pop(identifier, None)
            async with self.store.transaction():
                await self.store.db.execute(
                    "UPDATE agent_approvals SET state='expired' WHERE id=? AND state='pending'",
                    (identifier,),
                )

    async def list_pending(self, session: str) -> list[dict]:
        async with self.store.lock:
            cursor = await self.store.db.execute(
                "SELECT * FROM agent_approvals WHERE session_id=? AND state='pending'", (session,)
            )
            rows = await cursor.fetchall()
        return [
            {"id": row["id"], "run_id": row["run_id"], **json.loads(row["payload"])} for row in rows
        ]

    async def resolve(self, session: str, identifier: str, approved: bool) -> None:
        async with self.store.transaction():
            row = await self.store._one(
                "SELECT * FROM agent_approvals WHERE id=? AND session_id=? AND state='pending'",
                (identifier, session),
            )
            future = self.pending.get(identifier)
            if not row or future is None or future.done():
                raise AgentError("approval_expired", "此批准请求已结束。", 409)
            await self.store.db.execute(
                "UPDATE agent_approvals SET state=? WHERE id=?",
                ("approved" if approved else "denied", identifier),
            )
        future.set_result(approved)
