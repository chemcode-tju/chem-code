"""Transfer read-only Slurm observations into the platform before acknowledging them."""

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, PositiveInt, model_validator

from chemistry_agent.automation.slurm_remote import TERMINAL_STATES
from chemistry_agent.config import Settings

if TYPE_CHECKING:
    from chemistry_agent.store import Store

logger = logging.getLogger(__name__)
PAGE_SIZE = 100
TERMINAL_OBSERVATIONS = TERMINAL_STATES | {"SPECIAL_EXIT"}


class Observation(BaseModel):
    id: PositiveInt
    event: PositiveInt
    subscription: str = Field(min_length=1, max_length=100)
    consumer: str = Field(min_length=1, max_length=100)
    queue: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_fact(self):
        for key in ("job_id", "submitted", "state"):
            value = self.payload.get(key)
            if not isinstance(value, str) or not value or len(value) > 100:
                raise ValueError(f"Invalid Slurm observation field {key}")
        return self


class Page(BaseModel):
    hub_id: UUID
    claim: UUID | None
    deliveries: list[Observation] = Field(max_length=PAGE_SIZE)

    @model_validator(mode="after")
    def validate_page(self):
        identifiers = [item.id for item in self.deliveries]
        if bool(self.claim) != bool(identifiers) or identifiers != sorted(set(identifiers)):
            raise ValueError("Invalid claimed Slurm page")
        return self


def utc_timestamp(value: str) -> str:
    """Normalize Slurm's timezone-less UTC timestamps and explicit offsets."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


async def initialize(db) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS slurm_bridge_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            hub_id TEXT NOT NULL, event_id INTEGER NOT NULL,
            queue TEXT NOT NULL, job_id TEXT NOT NULL, submitted TEXT NOT NULL,
            name TEXT NOT NULL, terminal INTEGER NOT NULL, payload TEXT NOT NULL,
            UNIQUE(hub_id,event_id)
        );
        CREATE INDEX IF NOT EXISTS slurm_bridge_terminal_job
            ON slurm_bridge_events(queue,job_id,terminal,seq);
        CREATE TABLE IF NOT EXISTS slurm_bridge_deliveries (
            hub_id TEXT NOT NULL, delivery_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL, subscription_id TEXT NOT NULL,
            PRIMARY KEY(hub_id,delivery_id),
            FOREIGN KEY(hub_id,event_id) REFERENCES slurm_bridge_events(hub_id,event_id)
        );
    """)


async def persist_page(store: "Store", page: Page) -> None:
    """A crash cannot leave a partial platform page or acknowledge a conflicting ID."""
    hub = str(page.hub_id)
    async with store.transaction():
        for item in page.deliveries:
            fact = item.payload
            encoded = json.dumps(fact, sort_keys=True, separators=(",", ":"), allow_nan=False)
            terminal = fact["state"] in TERMINAL_OBSERVATIONS and fact.get("source") in (
                "sacct",
                "jobcomp/filetxt",
            )
            async with store.db.execute(
                """INSERT INTO slurm_bridge_events
                    (hub_id,event_id,queue,job_id,submitted,name,terminal,payload)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(hub_id,event_id) DO UPDATE SET payload=excluded.payload
                    WHERE queue=excluded.queue AND payload=excluded.payload RETURNING seq""",
                (
                    hub,
                    item.event,
                    item.queue,
                    fact["job_id"],
                    utc_timestamp(fact["submitted"]),
                    str(fact.get("name") or ""),
                    int(terminal),
                    encoded,
                ),
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise ValueError(f"Conflicting Slurm event {hub}/{item.event}")
            async with store.db.execute(
                """INSERT INTO slurm_bridge_deliveries VALUES(?,?,?,?)
                    ON CONFLICT(hub_id,delivery_id) DO UPDATE SET event_id=excluded.event_id
                    WHERE event_id=excluded.event_id AND subscription_id=excluded.subscription_id
                    RETURNING delivery_id""",
                (hub, item.id, item.event, item.subscription),
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise ValueError(f"Conflicting Slurm delivery {hub}/{item.id}")


class SlurmBridge:
    def __init__(self, settings: Settings, store: "Store") -> None:
        self.settings = settings
        self.store = store
        self.task: asyncio.Task | None = None
        self.hub_id: str | None = None
        self.queues_by_host: dict[str, list[str]] = {}
        for queue, host in settings.slurm_bridge_queue_hosts.items():
            self.queues_by_host.setdefault(host, []).append(queue)

    async def transfer_page(self, client: httpx.AsyncClient, limit: int = PAGE_SIZE) -> int:
        if not 1 <= limit <= PAGE_SIZE:
            raise ValueError(f"Slurm page limit must be between 1 and {PAGE_SIZE}")
        token_file = self.settings.slurm_bridge_token_file
        if token_file is None:
            raise ValueError("Slurm bridge token file is required")
        token = (await asyncio.to_thread(token_file.read_text)).strip()
        if not token:
            raise ValueError("Slurm bridge token file is empty")
        headers = {"Authorization": f"Bearer {token}"}
        consumer = self.settings.slurm_bridge_consumer
        base_url = self.settings.slurm_bridge_url.rstrip("/")
        response = await client.post(
            f"{base_url}/claim", headers=headers, json={"consumer": consumer, "limit": limit}
        )
        response.raise_for_status()
        page = Page.model_validate(response.json())
        if len(page.deliveries) > limit:
            raise ValueError("Slurm source exceeded the requested page limit")
        if any(item.consumer != consumer for item in page.deliveries):
            raise ValueError("Slurm page belongs to another consumer")
        self.hub_id = str(page.hub_id)
        if not page.claim:
            return 0
        await persist_page(self.store, page)
        response = await client.post(
            f"{base_url}/finish",
            headers=headers,
            json={"consumer": consumer, "claim": page.claim.hex, "success": True},
        )
        response.raise_for_status()
        if response.json() != {"ok": True}:
            raise ValueError("Slurm source did not confirm the ACK")
        return len(page.deliveries)

    async def expedite_inspection(self, state: dict) -> None:
        """Apply one trusted terminal hint to a matching registered attempt's polling timer."""
        job = state.get("job") or {}
        queues = self.queues_by_host.get(job.get("host"))
        if not self.hub_id or not queues or not job.get("scheduler_job_id"):
            return
        identity = job.get("identity") or {}
        submitted = utc_timestamp(identity.get("submitted_at") or "")
        name = "" if job.get("origin") == "adopted" else job.get("name") or ""
        if not (submitted or name):
            return
        placeholders = ",".join("?" for _ in queues)
        async with self.store.lock:
            row = await self.store._one(
                f"""SELECT seq FROM slurm_bridge_events WHERE hub_id=? AND terminal=1
                    AND queue IN ({placeholders}) AND job_id=? AND seq>?
                    AND ((?<>'' AND submitted=?) OR (?<>'' AND name=?))
                    ORDER BY seq DESC LIMIT 1""",
                (
                    self.hub_id,
                    *queues,
                    str(job["scheduler_job_id"]),
                    state.get("bridge_observation_seq", 0),
                    submitted,
                    submitted,
                    name,
                    name,
                ),
            )
        if row:
            state["bridge_observation_seq"] = row["seq"]
            state.pop("polled_at", None)

    async def watch(self) -> None:
        retry_seconds = 1
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            while True:
                try:
                    count = await self.transfer_page(client)
                except Exception as exc:
                    logger.warning("Slurm bridge delayed: %s", type(exc).__name__)
                    await asyncio.sleep(retry_seconds)
                    retry_seconds = min(30, retry_seconds * 2)
                else:
                    retry_seconds = 1
                    await asyncio.sleep(0 if count else 1)

    def start(self) -> None:
        self.task = asyncio.create_task(self.watch())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
