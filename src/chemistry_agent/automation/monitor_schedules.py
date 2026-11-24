"""Local monitor preferences and recurring wakes backed by the normal chat queue."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StringConstraints

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.routes import local_service
from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import ChatRequest
from chemistry_agent.store import Store

logger = logging.getLogger(__name__)
DEFAULT_INSTRUCTION = (
    "核对 Slurm 调度状态、退出码和实际输出，区分作业结束与科学收敛，汇总结果和缺失项。"
)
Instruction = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]


class MonitorDefaults(StrictModel):
    instruction: Instruction


class ScheduleFields(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    instruction: Instruction
    interval_minutes: int = Field(ge=1, le=525600, strict=True)


class Schedule(ScheduleFields):
    session_id: UUID
    enabled: bool


async def initialize(db) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS monitor_defaults (
            id INTEGER PRIMARY KEY CHECK(id=1), instruction TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS monitor_schedules (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            name TEXT NOT NULL, instruction TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
            enabled INTEGER NOT NULL, next_at INTEGER,
            last_request TEXT REFERENCES chat_requests(id)
        );
        CREATE TABLE IF NOT EXISTS monitor_occurrences (
            request_id TEXT PRIMARY KEY REFERENCES chat_requests(id) ON DELETE CASCADE,
            schedule_id TEXT NOT NULL, scheduled_at INTEGER NOT NULL,
            UNIQUE(schedule_id,scheduled_at)
        );
        CREATE INDEX IF NOT EXISTS chat_requests_queued ON chat_requests(status)
            WHERE status='queued';
        CREATE INDEX IF NOT EXISTS monitor_schedules_due ON monitor_schedules(next_at);
    """)
    async with db.execute("PRAGMA table_info(monitor_schedules)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
        if "created_by" not in columns:
            await db.execute(
                "ALTER TABLE monitor_schedules ADD COLUMN created_by TEXT NOT NULL DEFAULT 'user'"
            )
        if "revision" not in columns:
            await db.execute(
                "ALTER TABLE monitor_schedules ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )


async def slurm_instruction(store: Store) -> str:
    """Called under the store lock or within the event's transaction."""
    row = await store._one("SELECT instruction FROM monitor_defaults WHERE id=1")
    return row["instruction"] if row else DEFAULT_INSTRUCTION


def monitor_store(request: Request) -> Store:
    local_service(request)
    if not request.app.state.settings.automation_enabled:
        raise AgentError("automation_disabled", "请先启用计算平台。", 403)
    return request.app.state.store


router = APIRouter(prefix="/api/job-monitor/wakes", tags=["queue monitor"])
MonitorStore = Annotated[Store, Depends(monitor_store)]


@router.get("")
async def overview(store: MonitorStore, request: Request) -> dict:
    async with store.lock:
        async with store.db.execute(
            "SELECT m.*,s.title,q.status AS last_status,q.error AS last_error,"
            "q.created_at AS last_at FROM monitor_schedules m JOIN sessions s ON s.id=m.session_id "
            "LEFT JOIN chat_requests q ON q.id=m.last_request WHERE s.owner_id IS NULL "
            "ORDER BY m.rowid DESC"
        ) as cursor:
            schedules = [
                {**dict(row), "enabled": bool(row["enabled"])} for row in await cursor.fetchall()
            ]
        async with store.db.execute(
            "SELECT id,title FROM sessions WHERE owner_id IS NULL AND id NOT IN "
            "(SELECT session_id FROM task_agent_sessions) ORDER BY updated_at DESC"
        ) as cursor:
            sessions = [dict(row) for row in await cursor.fetchall()]
        return {
            "scheduler_enabled": request.app.state.settings.platform_agent_enabled,
            "instruction": await slurm_instruction(store),
            "factory_instruction": DEFAULT_INSTRUCTION,
            "schedules": schedules,
            "sessions": sessions,
        }


@router.get("/workflows")
async def workflow_list(
    store: MonitorStore, request: Request, session_id: UUID | None = None
) -> dict:
    from chemistry_agent.automation.workflow_monitors import workflows

    return await workflows(store, request.app.state.calculation_chat.root, session_id)


@router.put("/defaults")
async def defaults(body: MonitorDefaults, store: MonitorStore) -> dict:
    async with store.transaction():
        await store.db.execute(
            "INSERT OR REPLACE INTO monitor_defaults VALUES(1,?)", (body.instruction,)
        )
    return {"instruction": body.instruction}


async def check_revision(store: Store, identifier: UUID, revision: int | None) -> None:
    if revision is None:
        return
    row = await store._one("SELECT revision FROM monitor_schedules WHERE id=?", (str(identifier),))
    if revision != (row["revision"] if row else 0):
        raise AgentError("schedule_changed", "定时任务已变更，请重新打开任务查看最新设置。", 409)


@router.put("/schedules/{identifier}")
async def save_schedule(
    identifier: UUID, body: Schedule, store: MonitorStore, revision: int | None = None
) -> dict:
    async with store.transaction():
        await check_revision(store, identifier, revision)
        await write_schedule(store, identifier, body)
    return {"id": str(identifier)}


async def write_schedule(
    store: Store, identifier: UUID, body: Schedule, *, created_by: str = "user"
) -> None:
    """Write a schedule inside the caller's transaction, preserving its cadence on edits."""
    session = str(body.session_id)
    if not await store._one(
        "SELECT id FROM sessions WHERE id=? AND owner_id IS NULL AND id NOT IN "
        "(SELECT session_id FROM task_agent_sessions)",
        (session,),
    ):
        raise AgentError("session_not_found", "请选择可用的对话。", 404)
    due = int(time.time()) + body.interval_minutes * 60 if body.enabled else None
    await store.db.execute(
        """INSERT INTO monitor_schedules
           (id,session_id,name,instruction,interval_minutes,enabled,next_at,created_by)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
           session_id=excluded.session_id,name=excluded.name,instruction=excluded.instruction,
           revision=revision+1,enabled=excluded.enabled,interval_minutes=excluded.interval_minutes,
           last_request=CASE WHEN NOT enabled AND excluded.enabled AND (
                SELECT status FROM chat_requests WHERE id=last_request)='uncertain'
                THEN NULL ELSE last_request END,
           next_at=CASE WHEN enabled=excluded.enabled
                   AND interval_minutes=excluded.interval_minutes
                   THEN next_at ELSE excluded.next_at END""",
        (
            str(identifier),
            session,
            body.name,
            body.instruction,
            body.interval_minutes,
            body.enabled,
            due,
            created_by,
        ),
    )


@router.delete("/schedules/{identifier}")
async def remove_schedule(
    identifier: UUID, store: MonitorStore, revision: int | None = None
) -> dict:
    async with store.transaction():
        await check_revision(store, identifier, revision)
        await store.db.execute("DELETE FROM monitor_schedules WHERE id=?", (str(identifier),))
    return {"id": str(identifier)}


async def tick(app, now: int) -> None:
    """Advance each due clock and commit its chat receipt in the same transaction."""
    store = app.state.store
    queue = app.state.chat_queue
    async with store.transaction():
        async with store.db.execute(
            "SELECT m.*,r.status FROM monitor_schedules m JOIN sessions s ON s.id=m.session_id "
            "LEFT JOIN chat_requests r ON r.id=m.last_request WHERE m.enabled AND m.next_at<=? "
            "AND s.owner_id IS NULL ORDER BY m.next_at,m.rowid",
            (now,),
        ) as cursor:
            due = await cursor.fetchall()
        config = queue.provider.store.load(None) if due else None
        for row in due:
            period = row["interval_minutes"] * 60
            slot = now - (now - row["next_at"]) % period
            await store.db.execute(
                "UPDATE monitor_schedules SET next_at=? WHERE id=?", (slot + period, row["id"])
            )
            if row["status"] == "uncertain":
                await store.db.execute(
                    "UPDATE monitor_schedules SET enabled=0,next_at=NULL,revision=revision+1 "
                    "WHERE id=?",
                    (row["id"],),
                )
                continue
            if row["status"] in {"queued", "running"}:
                continue
            identifier = str(uuid5(UUID(row["id"]), str(slot)))
            if await store._one(
                "SELECT 1 FROM monitor_occurrences WHERE request_id=?", (identifier,)
            ):
                continue
            body = ChatRequest(
                session_id=row["session_id"],
                message=f"定时任务「{row['name']}」\n{row['instruction']}",
            )
            await queue.insert_receipt(
                identifier,
                row["session_id"],
                body,
                True,
                config.provider,
                config.base_url,
            )
            await store.db.execute(
                "INSERT INTO monitor_occurrences VALUES(?,?,?)", (identifier, row["id"], slot)
            )
            await store.db.execute(
                "UPDATE monitor_schedules SET last_request=? WHERE id=?", (identifier, row["id"])
            )
        async with store.db.execute(
            "SELECT q.id FROM chat_requests q JOIN monitor_occurrences m ON m.request_id=q.id "
            "WHERE q.status='queued' ORDER BY q.rowid"
        ) as cursor:
            receipts = [row[0] for row in await cursor.fetchall()]
    for identifier in receipts:
        if identifier not in queue.live:
            await queue.recover(app, identifier)


@asynccontextmanager
async def scheduler(app, enabled: bool) -> AsyncIterator[None]:
    async def watch() -> None:
        while True:
            try:
                await tick(app, int(time.time()))
            except Exception:
                logger.exception("定时唤醒检查失败")
            await asyncio.sleep(5)

    task = asyncio.create_task(watch()) if enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
