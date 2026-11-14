"""Native context telemetry and durable, exclusive Agent maintenance operations."""

import asyncio
import json

from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import ContextUsage, TokenUsage
from chemistry_agent.store import utc_now


def token_count(value: object) -> int | None:
    """Missing native measurements must not become a fabricated zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class NativeContext:
    def __init__(self, router) -> None:
        self.router, self.store = router, router.store
        self.tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        # Called inside the router's initialization transaction.
        await self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS native_context ("
            "session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE, "
            "backend TEXT, thread_id TEXT, payload TEXT NOT NULL, "
            "PRIMARY KEY(session_id,backend,thread_id))"
        )
        await self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS native_context_operations ("
            "id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE, "
            "backend TEXT, thread_id TEXT, kind TEXT, state TEXT, "
            "started_at TEXT, finished_at TEXT)"
        )
        cursor = await self.store.db.execute(
            "SELECT o.*,r.status AS run_status FROM native_context_operations o "
            "LEFT JOIN runs r ON r.id=o.id WHERE o.state='running'"
        )
        rows = await cursor.fetchall()
        for row in rows:
            await self.store.db.execute(
                "UPDATE native_context_operations SET state=?,finished_at=? WHERE id=?",
                (
                    "completed" if row["run_status"] == "completed" else "interrupted",
                    utc_now(),
                    row["id"],
                ),
            )
        # No automatic retry: the native process may have already compacted its history.
        cursor = await self.store.db.execute("SELECT * FROM native_context")
        rows = await cursor.fetchall()
        for row in rows:
            data = json.loads(row["payload"])
            if data.get("compaction", {}).get("status") == "running":
                data["compaction"].update(status="interrupted", finished_at=utc_now())
                await self._save(row["session_id"], row["backend"], row["thread_id"], data)

    async def _read(self, session: str, backend: str, thread: str) -> dict:
        row = await self.store._one(
            "SELECT payload FROM native_context WHERE session_id=? AND backend=? AND thread_id=?",
            (session, backend, thread),
        )
        return json.loads(row["payload"]) if row else {}

    async def _save(self, session: str, backend: str, thread: str, data: dict) -> None:
        await self.store.db.execute(
            "INSERT INTO native_context VALUES(?,?,?,?) ON CONFLICT(session_id,backend,thread_id) "
            "DO UPDATE SET payload=excluded.payload",
            (session, backend, thread, json.dumps(data, ensure_ascii=False)),
        )

    async def patch(self, session: str, backend: str, thread: str, **values) -> None:
        async with self.store.transaction():
            data = await self._read(session, backend, thread)
            data.update(values)
            await self._save(session, backend, thread, data)

    async def usage(
        self,
        session: str,
        backend: str,
        thread: str,
        used: object,
        capacity: object,
        *,
        turn_id: str | None = None,
    ) -> None:
        used, capacity = token_count(used), token_count(capacity)
        async with self.store.transaction():
            data = await self._read(session, backend, thread)
            data.update(used_tokens=used, capacity_tokens=capacity or None, updated_at=utc_now())
            compact = data.get("compaction", {})
            if used is not None and (
                compact.get("status") == "running"
                or (
                    compact.get("status") == "completed"
                    and turn_id
                    and compact.get("turn_id") == turn_id
                    and "after_tokens" not in compact
                )
            ):
                compact["after_tokens"] = used
            await self._save(session, backend, thread, data)

    async def compaction(
        self,
        session: str,
        backend: str,
        thread: str,
        status: str,
        *,
        event_id: str | None = None,
        manual: bool = False,
        turn_id: str | None = None,
    ) -> None:
        async with self.store.transaction():
            data = await self._read(session, backend, thread)
            current = data.get("compaction", {})
            if status == "running":
                if current.get("status") != "running":
                    current = {
                        "status": status,
                        "started_at": utc_now(),
                        "manual": manual,
                        "before_tokens": data.get("used_tokens"),
                    }
                if event_id:
                    current["event_id"] = event_id
                if turn_id:
                    current["turn_id"] = turn_id
            else:
                if (
                    event_id
                    and current.get("event_id") == event_id
                    and current.get("status") == status
                ):
                    return
                current.update(status=status, finished_at=utc_now())
                if event_id:
                    current["event_id"] = event_id
                # Until the Agent sends a post-compaction measurement, old usage is stale.
                if status == "completed" and "after_tokens" not in current:
                    data["used_tokens"] = None
            data["compaction"] = current
            await self._save(session, backend, thread, data)

    async def codex_event(self, session: str, thread: str, event: dict) -> None:
        params = event.get("params", {})
        if params.get("threadId", thread) != thread:
            return
        method = event.get("method")
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            await self.usage(
                session,
                "codex",
                thread,
                usage.get("last", {}).get("totalTokens"),
                usage.get("modelContextWindow"),
                turn_id=params.get("turnId"),
            )
        elif method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if item.get("type") == "contextCompaction":
                await self.compaction(
                    session,
                    "codex",
                    thread,
                    "running" if method == "item/started" else "completed",
                    event_id=item.get("id"),
                    turn_id=params.get("turnId"),
                )
        elif method == "turn/completed" and params.get("turn", {}).get("status") != "completed":
            await self.finish_pending(session, "codex", thread, "failed")

    async def acp_event(self, session: str, thread: str, event: dict) -> None:
        params = event.get("params", {})
        if event.get("method") != "session/update" or params.get("sessionId") != thread:
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "usage_update":
            await self.usage(session, "antigravity", thread, update.get("used"), update.get("size"))
        elif kind == "available_commands_update":
            await self.patch(
                session,
                "antigravity",
                thread,
                manual_supported=any(
                    c.get("name") == "compact" for c in update.get("availableCommands", [])
                ),
            )

    async def finish_pending(self, session: str, backend: str, thread: str, status: str) -> None:
        async with self.store.lock:
            data = await self._read(session, backend, thread)
        if data.get("compaction", {}).get("status") == "running":
            await self.compaction(session, backend, thread, status)

    async def snapshot(self, session: str, binding: dict) -> dict:
        backend, thread = binding["backend"], binding.get("external_thread_id")
        async with self.store.lock:
            data = await self._read(session, backend, thread) if thread else {}
            row = await self.store._one("SELECT active_run_id FROM sessions WHERE id=?", (session,))
            queued = await self.store._one(
                "SELECT id FROM chat_requests WHERE session_id=? "
                "AND status IN ('queued','running')",
                (session,),
            )
            operation = await self.store._one(
                "SELECT id,kind,state,started_at,finished_at FROM native_context_operations "
                "WHERE session_id=? "
                "AND backend=? AND thread_id=? ORDER BY started_at DESC LIMIT 1",
                (session, backend, thread),
            )
        return {
            **data,
            "managed_by": backend,
            "has_thread": bool(thread),
            "manual_supported": bool(thread) and data.get("manual_supported", backend == "codex"),
            "busy": bool(row and row["active_run_id"] or queued),
            **({"operation": dict(operation)} if operation else {}),
        }

    async def start(self, session: str, owner: str | None, kind: str, identifier: str) -> dict:
        """Atomically claim the same session lease as chat; retries reuse the receipt."""
        async with self.store.transaction():
            prior = await self.store._one(
                "SELECT * FROM native_context_operations WHERE id=?", (identifier,)
            )
            if prior:
                if prior["session_id"] != session or prior["kind"] != kind:
                    raise AgentError("context_request_conflict", "操作编号已被使用。", 409)
                return dict(prior)
            row = await self.store._one(
                "SELECT active_run_id FROM sessions WHERE id=? AND owner_id IS ?", (session, owner)
            )
            binding = await self.store._one(
                "SELECT * FROM agent_bindings WHERE session_id=?", (session,)
            )
            if row is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            queued = await self.store._one(
                "SELECT id FROM chat_requests WHERE session_id=? "
                "AND status IN ('queued','running')",
                (session,),
            )
            if row["active_run_id"] or queued:
                raise AgentError("session_busy", "本轮结束后可以压缩上下文。", 409)
            if not binding or not binding["external_thread_id"] or binding["backend"] == "builtin":
                raise AgentError("native_context_unavailable", "原生会话尚未建立。", 409)
            binding = dict(binding)
            data = await self._read(session, binding["backend"], binding["external_thread_id"])
            if kind == "compact" and not data.get(
                "manual_supported", binding["backend"] == "codex"
            ):
                raise AgentError("native_compact_unsupported", "此 Agent 未声明手动压缩能力。", 409)
            now = utc_now()
            await self.store.db.execute(
                "INSERT INTO native_context_operations VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    identifier,
                    session,
                    binding["backend"],
                    binding["external_thread_id"],
                    kind,
                    "running",
                    now,
                ),
            )
            await self.store.db.execute(
                "INSERT INTO runs(id,session_id,status,started_at,context_info) "
                "VALUES(?,?,'running',?,?)",
                (identifier, session, now, ContextUsage(manual_compaction=True).model_dump_json()),
            )
            await self.store.db.execute(
                "UPDATE sessions SET active_run_id=? WHERE id=?", (identifier, session)
            )
        task = asyncio.create_task(self._execute(session, identifier, binding, kind))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {"id": identifier, "kind": kind, "state": "running"}

    async def _execute(self, session: str, identifier: str, binding: dict, kind: str) -> None:
        backend, thread = binding["backend"], binding["external_thread_id"]
        status = "completed"
        try:
            async with asyncio.timeout(600):
                await self.router.adapters[backend].context_operation(session, binding, kind)
        except BaseException as exc:
            # An interrupted native request is never silently replayed.
            status = (
                "failed"
                if isinstance(exc, AgentError) and "disconnected" not in exc.code
                else "interrupted"
            )
            import logging

            logging.getLogger(__name__).warning("Native context %s %s: %s", kind, status, exc)
            if kind == "compact":
                await self.finish_pending(session, backend, thread, status)
            await self.store.fail_run(
                session,
                identifier,
                "native_context_" + status,
                tool_calls=[],
                usage=TokenUsage(),
                context=ContextUsage(manual_compaction=True),
            )
        else:
            await self.store.complete_run(
                session,
                identifier,
                [],
                "",
                [],
                TokenUsage(),
                context=ContextUsage(manual_compaction=True),
                touch_session=False,
            )
        finally:
            async with self.store.transaction():
                await self.store.db.execute(
                    "UPDATE native_context_operations SET state=?,finished_at=? WHERE id=?",
                    (status, utc_now(), identifier),
                )

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
