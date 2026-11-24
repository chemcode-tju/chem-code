"""Stable delivery receipts: accepting an input and finishing work are separate facts.

Inspired by DSH Super Agent's AgentDelivery/Effects boundaries; no upstream code copied.
"""

import json

from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.checkpoint import current_checkpoint
from chemistry_agent.schemas import TokenUsage, ToolExecution


class BackendDeliveries:
    def __init__(self, store) -> None:
        self.store = store

    async def initialize(self) -> None:
        await self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS backend_deliveries "
            "(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, backend TEXT NOT NULL, "
            "run_id TEXT NOT NULL, state TEXT NOT NULL, result TEXT)"
        )

    async def execute(self, adapter, messages: list[dict], progress, **kwargs) -> dict:
        checkpoint = current_checkpoint.get()
        direct_key = kwargs["run_id"]
        if member := kwargs["binding"].get("member_id"):
            direct_key = f"{direct_key}:{member}:{kwargs['binding']['group_turn']}"
        key = (
            f"{type(checkpoint).__name__}:{checkpoint.identifier}"
            if checkpoint
            else f"run:{direct_key}"
        )
        async with self.store.transaction():
            existing = await self.store._one("SELECT * FROM backend_deliveries WHERE id=?", (key,))
            if existing:
                if existing["session_id"] != kwargs["session_id"]:
                    raise AgentError("delivery_conflict", "消息投递归属冲突。", 409)
                if existing["state"] not in {"completed", "not_sent"}:
                    raise AgentError(
                        "agent_delivery_uncertain",
                        "这条消息已交给外部 Agent，结果待核对，不会重复发送。",
                        409,
                    )
                if existing["state"] == "completed":
                    result = json.loads(existing["result"])
                    return {
                        "messages": [*messages, *result["tail"]],
                        "tool_calls": [ToolExecution.model_validate(c) for c in result["calls"]],
                        "usage": TokenUsage(),
                    }
            await self.store.db.execute(
                "INSERT INTO backend_deliveries VALUES(?,?,?,?,?,NULL) "
                "ON CONFLICT(id) DO UPDATE SET state='started',run_id=excluded.run_id,result=NULL",
                (
                    key,
                    kwargs["session_id"],
                    kwargs["binding"]["backend"],
                    kwargs["run_id"],
                    "started",
                ),
            )
        try:
            result = await adapter.run(messages, progress, **kwargs)
            encoded = json.dumps(
                {
                    "tail": result["messages"][len(messages) :],
                    "calls": [c.model_dump() for c in result["tool_calls"]],
                }
            )
            async with self.store.transaction():
                await self.store.db.execute(
                    "UPDATE backend_deliveries SET state='completed',result=? WHERE id=?",
                    (encoded, key),
                )
            return result
        except BaseException as exc:
            async with self.store.transaction():
                await self.store.db.execute(
                    "UPDATE backend_deliveries SET state=?,result=? WHERE id=?",
                    (
                        "not_sent" if getattr(exc, "delivery_not_sent", False) else "uncertain",
                        json.dumps(
                            {
                                "error": {
                                    "code": getattr(exc, "code", type(exc).__name__),
                                    "message": str(exc)[:1000],
                                }
                            }
                        ),
                        key,
                    ),
                )
            if checkpoint and getattr(exc, "delivery_not_sent", False):
                # Native adapters persist a checkpoint before dispatch. Undo only its
                # replay boundary when we can prove no prompt was sent, not its data.
                await checkpoint.save(await checkpoint.load())
            raise
