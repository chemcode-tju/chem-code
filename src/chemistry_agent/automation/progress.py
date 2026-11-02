"""Session-owned, cursor-paged calculation progress. Reading never runs a calculation."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class ProgressStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS progress (
                    seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                    node_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL, event_key TEXT NOT NULL,
                    UNIQUE(session_id,plan_id,event_key));
                CREATE INDEX IF NOT EXISTS progress_session ON progress(session_id,seq);
                CREATE INDEX IF NOT EXISTS progress_node
                    ON progress(session_id,plan_id,node_id,seq);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def insert(
        db,
        session: str,
        plan: str,
        node: str | None,
        kind: str,
        status: str,
        message: str,
        detail: dict,
        key: str,
    ) -> None:
        db.execute(
            "INSERT INTO progress(session_id,plan_id,node_id,kind,status,message,detail,"
            "created_at,event_key) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id,plan_id,event_key) DO NOTHING",
            (
                session,
                plan,
                node,
                kind,
                status,
                message,
                json.dumps(detail, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
                key,
            ),
        )

    def timeline(
        self, session: str, after: int, limit: int, plan: str | None = None, node: str | None = None
    ) -> dict:
        clauses = ["session_id=?", "seq>?"]
        args = [session, after]
        for column, value in (("plan_id", plan), ("node_id", node)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,plan_id,node_id,kind,status,message,detail,created_at FROM progress "
                f"WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
        events = [dict(row) | {"detail": json.loads(row["detail"])} for row in rows[:limit]]
        return {
            "events": events,
            "next_cursor": events[-1]["seq"] if events else after,
            "has_more": len(rows) > limit,
        }
