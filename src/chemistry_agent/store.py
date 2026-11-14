import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite
from filelock import FileLock
from filelock import Timeout as LockTimeout

from chemistry_agent.attachments import init_attachment_schema
from chemistry_agent.auth import init_auth_schema
from chemistry_agent.errors import AgentError
from chemistry_agent.memory.service import init_memory_schema
from chemistry_agent.projects import init_project_schema
from chemistry_agent.runtime.checkpoint import UNCERTAIN
from chemistry_agent.schemas import ContextMemory, ContextUsage, TokenUsage, ToolExecution
from chemistry_agent.session_titles import init_title_schema


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Single-process SQLite storage with atomic per-session run reservations."""

    def __init__(self, db: aiosqlite.Connection, process_lock: FileLock, path: Path):
        self.attachment_dir = path.parent / (path.name + ".files")
        self.db = db
        self.lock = asyncio.Lock()
        self.process_lock = process_lock

    @classmethod
    async def open(cls, path: Path) -> "Store":
        path = await asyncio.to_thread(path.resolve)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        process_lock = FileLock(str(path) + ".lock")
        try:
            process_lock.acquire(timeout=0)
        except LockTimeout as exc:
            raise RuntimeError("此数据库已有 Chem Code 服务运行，请使用现有服务。") from exc
        try:
            db = await aiosqlite.connect(path)
        except BaseException:
            process_lock.release()
            raise
        db.row_factory = aiosqlite.Row
        try:
            await init_auth_schema(db)
            await db.executescript("""
                PRAGMA foreign_keys = ON;
                PRAGMA journal_mode = WAL;
                PRAGMA busy_timeout = 5000;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    active_run_id TEXT
                );
                CREATE TABLE IF NOT EXISTS session_models (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, seq);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    answer TEXT, error_code TEXT,
                    tool_calls TEXT NOT NULL DEFAULT '[]', usage TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS chat_requests (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    body TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, run_id TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS chat_requests_session ON chat_requests(session_id);
                CREATE TABLE IF NOT EXISTS chat_checkpoints (
                    request_id TEXT PRIMARY KEY REFERENCES chat_requests(id) ON DELETE CASCADE,
                    phase TEXT NOT NULL DEFAULT 'model', payload TEXT, local INTEGER NOT NULL,
                    provider TEXT NOT NULL, destination TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_memory (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    summary TEXT NOT NULL, covered_messages INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            async with db.execute("PRAGMA table_info(sessions)") as cursor:
                session_columns = {row["name"] for row in await cursor.fetchall()}
            if "owner_id" not in session_columns:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN owner_id TEXT REFERENCES users(id)"
                )
            await db.execute("CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner_id)")
            async with db.execute("PRAGMA table_info(runs)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            if "context_info" not in columns:
                await db.execute(
                    "ALTER TABLE runs ADD COLUMN context_info TEXT NOT NULL DEFAULT '{}'"
                )
            async with db.execute("PRAGMA table_info(messages)") as cursor:
                if "task_event" not in {row["name"] for row in await cursor.fetchall()}:
                    await db.execute("ALTER TABLE messages ADD COLUMN task_event TEXT")
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS messages_task_event ON messages(task_event)"
            )
            await init_project_schema(db)
            await init_memory_schema(db)
            await init_attachment_schema(db)
            await init_title_schema(db)
            from chemistry_agent.automation.wait_ledger import init_wait_schema

            await init_wait_schema(db)
            # End old run leases but preserve replayable request checkpoints.
            await db.execute(
                "UPDATE runs SET status='interrupted', error_code='service_restarted', "
                "finished_at=? WHERE status='running'",
                (utc_now(),),
            )
            await db.execute("UPDATE sessions SET active_run_id=NULL")
            # Reconcile orphaned deliveries at startup even for paused plans, whose
            # dispatcher intentionally never polls pending events. Unknown tool
            # effects remain uncertain; no side effect is replayed here.
            await db.execute(
                "UPDATE agent_events SET status=CASE "
                "WHEN EXISTS(SELECT 1 FROM runs WHERE id=run_id AND status='completed') "
                "THEN 'completed' WHEN phase='model' THEN 'pending' ELSE 'uncertain' END, "
                "error='service_restarted' WHERE status='running' AND NOT EXISTS "
                "(SELECT 1 FROM runs WHERE id=run_id AND status='running')"
            )
            await db.execute(
                "UPDATE chat_requests SET status='uncertain',error=? WHERE status='running' AND "
                "NOT EXISTS(SELECT 1 FROM chat_checkpoints c WHERE c.request_id=chat_requests.id "
                "AND c.phase='model')",
                (UNCERTAIN,),
            )
            await db.execute(
                "UPDATE chat_requests SET status='queued',run_id=NULL WHERE status='running'"
            )
            # Previous releases offered blind retries after a service restart, even after
            # a command may have started. Never replay such a legacy run without evidence.
            await db.execute(
                "UPDATE chat_requests SET status='uncertain',error=? WHERE status='failed' "
                "AND run_id IS NOT NULL AND error LIKE '服务已重启%' AND NOT EXISTS "
                "(SELECT 1 FROM chat_checkpoints c WHERE c.request_id=chat_requests.id)",
                (UNCERTAIN,),
            )
            await db.commit()
        except BaseException:
            await db.close()
            process_lock.release()
            raise
        return cls(db, process_lock, path)

    async def close(self) -> None:
        try:
            await self.db.close()
        finally:
            self.process_lock.release()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                # Cancellation can arrive after SQLite executed BEGIN but before
                # aiosqlite delivered its result. BEGIN must be covered by rollback.
                await self.db.execute("BEGIN IMMEDIATE")
                yield
                await self.db.commit()
            except BaseException:
                rollback = asyncio.create_task(self.db.rollback())
                try:
                    await asyncio.shield(rollback)
                except asyncio.CancelledError:
                    await rollback
                raise

    async def _one(self, sql: str, parameters: tuple = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def _history(self, session_id: str, *, display: bool = False) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT payload,task_event FROM messages WHERE session_id=? ORDER BY seq", (session_id,)
        ) as cursor:
            return [
                json.loads(row["payload"])
                | ({"task_event": row["task_event"]} if display and row["task_event"] else {})
                for row in await cursor.fetchall()
            ]

    async def append_task_report(self, session: str, event: str, answer: str) -> bool:
        """Commit an assistant-only message once, between user turns."""
        async with self.transaction():
            row = await self._one(
                "SELECT active_run_id FROM sessions WHERE id=? AND owner_id IS NULL", (session,)
            )
            if row is None or row["active_run_id"]:
                return False
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO messages(session_id,payload,task_event) VALUES(?,?,?)",
                (
                    session,
                    json.dumps({"role": "assistant", "content": answer}, ensure_ascii=False),
                    event,
                ),
            )
            if cursor.rowcount:
                await self.db.execute(
                    "UPDATE sessions SET updated_at=? WHERE id=?", (utc_now(), session)
                )
        return True

    async def create_session(self, title: str, owner_id: str | None = None) -> dict[str, Any]:
        session_id, now = str(uuid4()), utc_now()
        async with self.transaction():
            await self.db.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at, owner_id, summary_title) "
                "VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    title,
                    now,
                    now,
                    owner_id,
                    title if title in {"新对话", "结构与计算"} else None,
                ),
            )
        return {"id": session_id, "title": title, "created_at": now, "updated_at": now}

    async def list_sessions(
        self, limit: int, offset: int, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with (
            self.lock,
            self.db.execute(
                "SELECT id, COALESCE(summary_title,title) title, created_at, updated_at, "
                "(summary_title IS NOT NULL AND summary_ready=0) title_pending FROM sessions "
                "WHERE owner_id IS ? ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (owner_id, limit, offset),
            ) as cursor,
        ):
            return [dict(row) for row in await cursor.fetchall()]

    async def require_session(self, session_id: str, owner_id: str | None = None) -> None:
        """Check ownership without parsing the entire conversation for a metadata request."""
        async with self.lock:
            row = await self._one(
                "SELECT id FROM sessions WHERE id=? AND owner_id IS ?", (session_id, owner_id)
            )
        if row is None:
            raise AgentError("session_not_found", "会话不存在。", 404)

    async def get_session(self, session_id: str, owner_id: str | None = None) -> dict[str, Any]:
        async with self.lock:
            row = await self._one(
                "SELECT id, COALESCE(summary_title,title) title, created_at, updated_at, "
                "(summary_title IS NOT NULL AND summary_ready=0) title_pending FROM sessions "
                "WHERE id=? AND owner_id IS ?",
                (session_id, owner_id),
            )
            if row is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            history = await self._history(session_id, display=True)
        # Internal model reasoning is persisted only for the provider continuation protocol.
        messages = [
            {k: v for k, v in message.items() if k != "reasoning_content"} for message in history
        ]
        return {**dict(row), "messages": messages}

    async def get_session_model(
        self, session_id: str, owner_id: str | None, provider: str, base_url: str
    ) -> str | None:
        """Ignore preferences saved for a different provider or API destination."""
        async with self.lock:
            row = await self._one(
                "SELECT m.provider,m.base_url,m.model FROM sessions s "
                "LEFT JOIN session_models m ON m.session_id=s.id WHERE s.id=? AND s.owner_id IS ?",
                (session_id, owner_id),
            )
            if row is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            if row["provider"] != provider or (
                provider != "chatgpt" and row["base_url"] != base_url
            ):
                return None
            return row["model"]

    async def set_session_model(
        self,
        session_id: str,
        owner_id: str | None,
        provider: str,
        base_url: str,
        model: str | None,
    ) -> None:
        async with self.transaction():
            if (
                await self._one(
                    "SELECT id FROM sessions WHERE id=? AND owner_id IS ?", (session_id, owner_id)
                )
                is None
            ):
                raise AgentError("session_not_found", "会话不存在。", 404)
            await self.db.execute(
                "INSERT INTO session_models VALUES (?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "provider=excluded.provider,base_url=excluded.base_url,model=excluded.model",
                (session_id, provider, base_url, model),
            )

    async def begin_run(
        self,
        session_id: str,
        owner_id: str | None = None,
        *,
        request_id: str | None = None,
        file_ids: tuple[UUID, ...] = (),
        event_id: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        run_id = str(uuid4())
        async with self.transaction():
            row = await self._one(
                "SELECT active_run_id FROM sessions WHERE id=? AND owner_id IS ?",
                (session_id, owner_id),
            )
            if row is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            if row["active_run_id"] is not None:
                raise AgentError("session_busy", "上一条对话仍在生成回答，请等待处理完成。", 409)
            if event_id:
                queued = await self._one(
                    "SELECT id FROM chat_requests WHERE session_id=? AND status='queued' LIMIT 1",
                    (session_id,),
                )
                if queued:
                    raise AgentError("session_busy", "先处理已经排队的用户消息。", 409)
                cursor = await self.db.execute(
                    "UPDATE agent_events SET status='running',run_id=? "
                    "WHERE id=? AND session_id=? AND status IN ('pending','retry_wait')",
                    (run_id, event_id, session_id),
                )
                if cursor.rowcount != 1:
                    raise AgentError("event_already_claimed", "事件已被处理或取消。", 409)
                await self.db.execute(
                    "UPDATE agent_events SET run_id=? WHERE batch_parent=? AND status='bundled'",
                    (run_id, event_id),
                )
            await self.db.execute(
                "INSERT INTO runs(id, session_id, status, started_at) VALUES (?, ?, 'running', ?)",
                (run_id, session_id, utc_now()),
            )
            await self.db.execute(
                "UPDATE sessions SET active_run_id=? WHERE id=?", (run_id, session_id)
            )
            if file_ids:
                await self.db.executemany(
                    "INSERT OR IGNORE INTO session_files(session_id,file_id) VALUES(?,?)",
                    [(session_id, str(file_id)) for file_id in file_ids],
                )
            if request_id:
                await self.db.execute(
                    "UPDATE chat_requests SET status='running',run_id=? "
                    "WHERE id=? AND session_id=?",
                    (run_id, request_id, session_id),
                )
            history = await self._history(session_id)
        return run_id, history

    async def complete_run(
        self,
        session_id: str,
        run_id: str,
        messages: list[dict[str, Any]],
        answer: str,
        tool_calls: list[ToolExecution],
        usage: TokenUsage,
        context: ContextUsage | None = None,
        *,
        touch_session: bool = True,
    ) -> None:
        now = utc_now()
        async with self.transaction():
            await self.db.executemany(
                "INSERT INTO messages(session_id, payload) VALUES (?, ?)",
                [(session_id, json.dumps(message, ensure_ascii=False)) for message in messages],
            )
            await self.db.execute(
                "UPDATE runs SET status='completed', finished_at=?, answer=?, tool_calls=?, "
                "usage=?, context_info=? WHERE id=?",
                (
                    now,
                    answer,
                    json.dumps([call.model_dump() for call in tool_calls], ensure_ascii=False),
                    usage.model_dump_json(),
                    (context or ContextUsage()).model_dump_json(),
                    run_id,
                ),
            )
            await self.db.execute(
                "DELETE FROM chat_checkpoints WHERE request_id IN "
                "(SELECT id FROM chat_requests WHERE run_id=?)",
                (run_id,),
            )
            await self.db.execute(
                "UPDATE chat_requests SET status='completed',error=NULL WHERE run_id=?",
                (run_id,),
            )
            await self.db.execute(
                "UPDATE agent_events SET status='completed',error=NULL WHERE run_id=?",
                (run_id,),
            )
            await self.db.execute(
                "UPDATE sessions SET updated_at=CASE WHEN ? THEN ? ELSE updated_at END, "
                "active_run_id=NULL WHERE id=? AND active_run_id=?",
                (touch_session, now, session_id, run_id),
            )

    async def fail_run(
        self,
        session_id: str,
        run_id: str,
        code: str,
        *,
        tool_calls: list[ToolExecution],
        usage: TokenUsage,
        cancelled=False,
        context: ContextUsage | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self.transaction():
            old = await self._one("SELECT context_info FROM runs WHERE id=?", (run_id,))
            prior = json.loads(old["context_info"]) if old else {}
            details = (context or ContextUsage()).model_dump()
            details["automation_event"] = details.get("automation_event") or prior.get(
                "automation_event"
            )
            if error_message:
                details["error_message"] = error_message[:1000]
            await self.db.execute(
                "UPDATE runs SET status=?, finished_at=?, error_code=?, tool_calls=?, usage=?, "
                "context_info=? WHERE id=? AND status='running'",
                (
                    "cancelled" if cancelled else "failed",
                    utc_now(),
                    code,
                    json.dumps([call.model_dump() for call in tool_calls], ensure_ascii=False),
                    usage.model_dump_json(),
                    json.dumps(details),
                    run_id,
                ),
            )
            await self.db.execute(
                "UPDATE sessions SET active_run_id=NULL WHERE id=? AND active_run_id=?",
                (session_id, run_id),
            )
            if not details.get("manual_compaction"):
                await self.db.execute(
                    "UPDATE sessions SET updated_at=? WHERE id=?", (utc_now(), session_id)
                )

    async def get_run(self, run_id: str, owner_id: str | None = None) -> dict[str, Any]:
        async with self.lock:
            row = await self._one(
                "SELECT runs.* FROM runs JOIN sessions ON sessions.id=runs.session_id "
                "WHERE runs.id=? AND sessions.owner_id IS ?",
                (run_id, owner_id),
            )
        if row is None:
            raise AgentError("run_not_found", "运行记录不存在。", 404)
        return self._run_data(row)

    async def list_runs(
        self,
        session_id: str,
        limit: int,
        offset: int,
        owner_id: str | None = None,
        *,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        async with self.lock:
            if (
                await self._one(
                    "SELECT id FROM sessions WHERE id=? AND owner_id IS ?", (session_id, owner_id)
                )
                is None
            ):
                raise AgentError("session_not_found", "会话不存在。", 404)
            columns = (
                "id,session_id,status,started_at,finished_at,answer,error_code,usage,context_info,"
                "CASE WHEN status='running' THEN tool_calls ELSE '[]' END AS tool_calls"
                if compact
                else "*"
            )
            async with self.db.execute(
                f"SELECT {columns} FROM runs WHERE session_id=? ORDER BY started_at DESC, id "
                "LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ) as cursor:
                return [self._run_data(row) for row in await cursor.fetchall()]

    @staticmethod
    def _run_data(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        result["tool_calls"] = json.loads(result["tool_calls"])
        result["usage"] = json.loads(result["usage"])
        result["context"] = json.loads(result.pop("context_info", "{}"))
        return result

    async def get_context_memory(self, session_id: str) -> ContextMemory:
        async with self.lock:
            row = await self._one(
                "SELECT summary, covered_messages FROM context_memory WHERE session_id=?",
                (session_id,),
            )
        return ContextMemory(**dict(row)) if row else ContextMemory()

    async def save_context_memory(
        self, session_id: str, run_id: str, memory: ContextMemory
    ) -> None:
        async with self.transaction():
            row = await self._one("SELECT active_run_id FROM sessions WHERE id=?", (session_id,))
            if row is None or row["active_run_id"] != run_id:
                raise AgentError("session_reservation_lost", "会话运行状态已改变，请重试。", 409)
            await self.db.execute(
                "INSERT INTO context_memory(session_id, summary, covered_messages, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "summary=excluded.summary, covered_messages=excluded.covered_messages, "
                "updated_at=excluded.updated_at",
                (session_id, memory.summary, memory.covered_messages, utc_now()),
            )
