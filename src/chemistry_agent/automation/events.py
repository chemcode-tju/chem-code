"""Durable local task snapshots, notifications and recovery reservations."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def task_state(job: dict) -> tuple[str, str]:
    if job.get("finished_ok"):
        status = job.get("results", {}).get("run_status", {})
        ionic = status.get("nsw", 0) > 0
        if status.get("electronic_converged") is False or (
            ionic and status.get("ionic_converged") is False
        ):
            return "unconverged", "计算结束，但未收敛"
        if job.get("results") and (
            status.get("electronic_converged") is not True
            or (ionic and status.get("ionic_converged") is not True)
        ):
            return "needs_review", "计算结束，收敛证据待核验"
        if not job.get("results") and not job.get("report"):
            return "needs_review", "计算进程结束，结果待核验"
        return "completed", "计算完成，结果已取回"
    if job.get("process_state") in {"finished", "excepted", "killed"}:
        return "failed", "计算异常结束"
    if job.get("submission_state") == "rejected_by_scheduler":
        return "submission_rejected", "调度器拒绝提交，尚未进入队列"
    if job.get("paused"):
        return "paused", "任务已暂停"
    for child in job.get("steps", []):
        state, title = task_state(child)
        if state in {"failed", "unconverged", "needs_review"} and job.get("workflow"):
            return "active", f"子任务 #{child['pk']} 已结束，等待流程汇总"
        if state in FAILURE_STATES:
            return state, f"流程子任务 #{child['pk']}：{title}"
    if job.get("scheduler_state") == "running":
        return "running", "计算运行中"
    if job.get("scheduler_job_id"):
        return "queued", "已交给调度器，等待运行或状态更新"
    if job.get("workflow"):
        return "active", job.get("process_status") or "工作流正在推进"
    return "preparing", "AiiDA 正在准备或提交"


FAILURE_STATES = {"failed", "unconverged", "submission_rejected", "paused", "needs_review"}


class EventStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS snapshots (
                    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, preview_id TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, job_id TEXT,
                    session_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT
                );
                CREATE TABLE IF NOT EXISTS recoveries (
                    job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, preview_id TEXT NOT NULL,
                    state TEXT NOT NULL, decision TEXT, result TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    event_key TEXT PRIMARY KEY REFERENCES events(event_key),
                    preview_id TEXT NOT NULL,
                    answer TEXT, delivered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS policies (
                    job_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS retry_budget (
                    root_id TEXT NOT NULL, source_job_id TEXT PRIMARY KEY,
                    preview_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute(
                "UPDATE recoveries SET state='interrupted' WHERE state IN ('running','diagnosed')"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialized(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone())

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def notify(
        self, session: str, job_id: str, kind: str, title: str, detail: dict, key: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,NULL)",
                (
                    str(uuid4()),
                    key,
                    job_id,
                    session,
                    kind,
                    title,
                    json.dumps(detail, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def observe(
        self,
        session: str,
        preview_id: str,
        job: dict,
        *,
        diagnose: bool = False,
        report: bool = False,
    ) -> bool:
        state, title = task_state(job)
        snapshot = {**job, "display_state": state, "display_title": title}
        with self.connect() as db:
            before = db.execute(
                "SELECT state FROM snapshots WHERE job_id=?", (job["id"],)
            ).fetchone()
            db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                (
                    job["id"],
                    session,
                    preview_id,
                    state,
                    json.dumps(snapshot, ensure_ascii=False),
                    utc_now(),
                ),
            )
            changed = not before or before["state"] != state
            if changed and state in FAILURE_STATES | {"completed"}:
                # Snapshot, notification and wakeup commit together to avoid crash gaps.
                key = hashlib.sha256(f"{job['id']}:{state}".encode()).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (
                        str(uuid4()),
                        key,
                        job["id"],
                        session,
                        state,
                        f"AiiDA #{job['pk']} · {title}",
                        json.dumps(snapshot, ensure_ascii=False),
                        utc_now(),
                    ),
                )
                if report or (before and state != before["state"]):
                    db.execute(
                        "INSERT OR IGNORE INTO reports(event_key,preview_id) VALUES(?,?)",
                        (key, preview_id),
                    )
                if diagnose and state in FAILURE_STATES:
                    if before and before["state"] in FAILURE_STATES and state == "failed":
                        # A previously paused child may have stopped the first diagnosis.
                        db.execute(
                            "DELETE FROM recoveries WHERE job_id=? AND state='needs_user' "
                            "AND NOT EXISTS(SELECT 1 FROM retry_budget WHERE source_job_id=?) "
                            "AND json_extract(result,'$.preview_id') IS NULL",
                            (job["id"], job["id"]),
                        )
                    db.execute(
                        "INSERT OR IGNORE INTO recoveries VALUES (?,?,?,'queued',NULL,NULL,?)",
                        (job["id"], session, preview_id, utc_now()),
                    )
        return changed

    def pending_reports(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT e.*,r.preview_id,r.answer FROM reports r "
                "JOIN events e USING(event_key) WHERE delivered=0 ORDER BY created_at LIMIT 50"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_report(self, key: str, answer: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE reports SET answer=? WHERE event_key=?", (answer, key))

    def deliver_report(self, key: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE reports SET delivered=(SELECT MAX(delivered)+1 FROM reports) "
                "WHERE event_key=? AND delivered=0",
                (key,),
            )

    def latest_report(self, session: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT r.event_key FROM reports r JOIN events e USING(event_key) "
                "WHERE e.session_id=? AND delivered>0 ORDER BY delivered DESC LIMIT 1",
                (session,),
            ).fetchone()
        return row[0] if row else None

    def snapshots(self, session: str) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT job_id,payload FROM snapshots WHERE session_id=?", (session,)
            ).fetchall()
        return {row["job_id"]: json.loads(row["payload"]) for row in rows}

    def inbox(self, session: str | None, limit: int = 50) -> dict:
        where, values = ("WHERE session_id=?", [session]) if session else ("", [])
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?", [*values, limit]
            ).fetchall()
            unread = db.execute(
                f"SELECT COUNT(*) FROM events {where + ' AND' if where else 'WHERE'} "
                "read_at IS NULL",
                values,
            ).fetchone()[0]
            meta = dict(db.execute("SELECT key,value FROM meta").fetchall())
        return {
            "events": [{**dict(row), "detail": json.loads(row["detail"])} for row in rows],
            "unread": unread,
            "monitor": meta,
        }

    def mark_read(self, identifier: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE events SET read_at=? WHERE id=?", (utc_now(), identifier))

    def policy(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM policies WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else {"mode": "diagnose", "max_attempts": 2}

    def set_policy(self, job_id: str, policy: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO policies VALUES (?,?)", (job_id, json.dumps(policy)))

    def reserve_retry(self, root_id: str, job_id: str, preview_id: str, limit: int) -> bool:
        """Consume the root task's budget once, including uncertain submissions."""
        if limit <= 0:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT root_id,preview_id FROM retry_budget WHERE source_job_id=?", (job_id,)
            ).fetchone()
            if existing:
                return existing["root_id"] == root_id and existing["preview_id"] == preview_id
            used = db.execute(
                "SELECT COUNT(*) FROM retry_budget WHERE root_id=?", (root_id,)
            ).fetchone()[0]
            if used >= limit:
                return False
            db.execute(
                "INSERT INTO retry_budget VALUES (?,?,?,?)",
                (root_id, job_id, preview_id, utc_now()),
            )
            return True

    def enqueue(self, session: str, preview_id: str, job_id: str, *, force: bool = False) -> None:
        with self.connect() as db:
            if force:
                row = db.execute("SELECT * FROM recoveries WHERE job_id=?", (job_id,)).fetchone()
                if row and row["state"] in {"interrupted", "disabled", "needs_user"}:
                    result = json.loads(row["result"] or "{}")
                    if not result.get("preview_id"):
                        db.execute("DELETE FROM recoveries WHERE job_id=?", (job_id,))
            db.execute(
                "INSERT OR IGNORE INTO recoveries VALUES (?,?,?,'queued',NULL,NULL,?)",
                (job_id, session, preview_id, utc_now()),
            )

    def next_recovery(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM recoveries WHERE state IN ('queued','submitting') "
                "ORDER BY updated_at LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def recovery(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM recoveries WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "decision": json.loads(row["decision"]) if row["decision"] else None,
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def update_recovery(
        self, job_id: str, state: str, *, decision: dict | None = None, result: dict | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE recoveries SET state=?,decision=COALESCE(?,decision),"
                "result=COALESCE(?,result),updated_at=? WHERE job_id=?",
                (
                    state,
                    json.dumps(decision, ensure_ascii=False) if decision is not None else None,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    utc_now(),
                    job_id,
                ),
            )
