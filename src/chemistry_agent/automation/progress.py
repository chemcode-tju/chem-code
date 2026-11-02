"""Session-owned, cursor-paged calculation progress. Reading never runs a calculation."""

import json
import sqlite3
import time
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
                CREATE TABLE IF NOT EXISTS step_summaries (
                    step_seq INTEGER PRIMARY KEY REFERENCES progress(seq), evidence TEXT,
                    lease_until INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS step_summary_pending ON step_summaries(lease_until)
                    WHERE evidence IS NOT NULL;
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

    def queue_summary(
        self, db: sqlite3.Connection, session: str, plan: str, key: str, evidence: dict
    ) -> None:
        """Reserve one result interpretation in the same transaction as its step event."""
        db.execute(
            "INSERT OR IGNORE INTO step_summaries(step_seq,evidence) "
            "SELECT seq,? FROM progress WHERE session_id=? AND plan_id=? AND event_key=?",
            (json.dumps(evidence, ensure_ascii=False), session, plan, key),
        )

    def claim_summary(self) -> dict | None:
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT p.seq,p.session_id,p.plan_id,p.node_id,p.detail,q.evidence "
                "FROM step_summaries q JOIN progress p ON p.seq=q.step_seq "
                "WHERE q.evidence IS NOT NULL AND q.lease_until<=? ORDER BY q.step_seq LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            # Model timeout is 120 seconds. A lost worker's request becomes claimable again.
            expiry = now + 300
            db.execute(
                "UPDATE step_summaries SET lease_until=? WHERE step_seq=?", (expiry, row["seq"])
            )
            return dict(row) | {
                "lease_until": expiry,
                "evidence": json.loads(row["evidence"]),
                "detail": json.loads(row["detail"]),
            }

    def finish_summary(self, item: dict, status: str, answer: str) -> None:
        with self.connect() as db:
            updated = db.execute(
                "UPDATE step_summaries SET evidence=NULL WHERE step_seq=? AND lease_until=? "
                "AND evidence IS NOT NULL",
                (item["seq"], item["lease_until"]),
            )
            if updated.rowcount:
                detail = {
                    k: item["detail"][k] for k in ("attempt", "pk", "job_id", "scheduler_job_id")
                }
                self.insert(
                    db,
                    item["session_id"],
                    item["plan_id"],
                    item["node_id"],
                    "interpretation",
                    status,
                    answer,
                    detail | {"step_seq": item["seq"]},
                    f"summary:{item['seq']}",
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
