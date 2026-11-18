"""Restartable projection into OpenViking; errors never roll back scientific records."""

import asyncio
import hashlib
import logging
from contextlib import suppress

from chemistry_agent.knowledge.openviking import OpenViking, evidence_document
from chemistry_agent.memory.scope import MemoryScope

logger = logging.getLogger(__name__)


class KnowledgeSync:
    def __init__(self, store, client: OpenViking) -> None:
        self.store, self.client = store, client
        self.task = None
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.store.transaction():
            await self.store.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_projection (
                destination TEXT NOT NULL, memory_id TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(destination, memory_id))""")

    async def sync(self) -> dict:
        """Content-addressed upserts are retryable, unlike scheduler submissions."""
        if not self.client.config.read().enabled:
            return {"enabled": False, "updated": 0}
        async with self.lock:
            return await self._sync()

    async def _sync(self) -> dict:
        config = self.client.config.read()
        key = config.api_key.get_secret_value() if config.api_key else ""
        destination = hashlib.sha256(
            f"{config.endpoint}|{config.root_uri}|{key}".encode()
        ).hexdigest()
        async with self.store.lock:
            async with self.store.db.execute(
                """SELECT m.*, p.digest AS synced_digest
                FROM research_memories m LEFT JOIN knowledge_projection p
                ON p.memory_id=m.id AND p.destination=?
                WHERE p.digest IS NULL OR p.digest != m.digest || ':' || m.forgotten
                LIMIT 50""",
                (destination,),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        updated = 0
        for row in rows:
            scope = MemoryScope(row["owner_id"], row["project_id"])
            path = f"research/{row['id']}.md"
            if row["forgotten"]:
                await self.client.delete(scope, path)
            else:
                await self.client.write(scope, path, evidence_document(row))
            digest = f"{row['digest']}:{row['forgotten']}"
            async with self.store.transaction():
                await self.store.db.execute(
                    """INSERT INTO knowledge_projection VALUES(?,?,?)
                    ON CONFLICT(destination,memory_id) DO UPDATE SET digest=excluded.digest""",
                    (destination, row["id"], digest),
                )
            updated += 1
        return {"enabled": True, "updated": updated, "more": len(rows) == 50}

    def start(self) -> None:
        self.task = asyncio.create_task(self.loop())

    async def loop(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                logger.warning("OpenViking projection delayed; source records retained")
            await asyncio.sleep(60)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
