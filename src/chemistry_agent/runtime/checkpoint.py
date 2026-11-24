"""A model boundary is replayable; a tool with an unobserved outcome is not."""

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chemistry_agent.store import Store


UNSTARTED_CHECKPOINT = "(c.payload IS NULL OR json_extract(c.payload,'$.group.queued')=1)"


@dataclass
class Checkpoint:
    store: "Store"
    identifier: str

    async def load(self) -> dict:
        async with self.store.lock:
            row = await self.store._one(
                "SELECT payload FROM chat_checkpoints WHERE request_id=?", (self.identifier,)
            )
        return json.loads(row["payload"]) if row and row["payload"] else {}

    async def mark_tools(
        self, intent: list[dict] | None = None, *, payload: dict | None = None
    ) -> None:
        async with self.store.transaction():
            await self.store.db.execute(
                "UPDATE chat_checkpoints SET phase='tools',payload=COALESCE(?,payload) "
                "WHERE request_id=?",
                (json.dumps(payload, ensure_ascii=False) if payload else None, self.identifier),
            )

    async def save(self, payload: dict) -> None:
        async with self.store.transaction():
            await self.store.db.execute(
                "UPDATE chat_checkpoints SET phase='model',payload=? WHERE request_id=?",
                (json.dumps(payload, ensure_ascii=False), self.identifier),
            )


current_checkpoint: ContextVar[Checkpoint | None] = ContextVar("checkpoint", default=None)
UNCERTAIN = "工具执行时中断，结果未知。请先核对服务器或方案记录，原消息不会自动重放。"
