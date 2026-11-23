"""Crash-safe observation and one-shot waits, independent of any agent process."""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from chemistry_agent.research_plugins.models import JobRegister, WaitControl, WaitRegister

TERMINAL = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
)


def stable_id(value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"chem-research:{value}"))


class MonitorLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, exit_code TEXT, due REAL, observed REAL,
                    error TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS waits (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, thread TEXT NOT NULL,
                    jobs TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, wait_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, acknowledged REAL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS wakeups (
                    wait_id TEXT PRIMARY KEY REFERENCES waits(id),
                    instruction TEXT NOT NULL, project_path TEXT);
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY REFERENCES events(id),
                    thread_id TEXT NOT NULL, message_id TEXT NOT NULL, prompt TEXT NOT NULL,
                    status TEXT NOT NULL, submission_id TEXT, turn_id TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, error TEXT);
                CREATE INDEX IF NOT EXISTS waits_workspace_created ON waits(workspace, created);
                CREATE INDEX IF NOT EXISTS waits_status ON waits(status);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def job(row: sqlite3.Row) -> dict:
        return json.loads(row["request"]) | {
            "id": row["id"],
            "scheduler_state": row["state"],
            "exit_code": row["exit_code"],
            "observed_at": row["observed"],
            "observation_error": row["error"],
            "terminal": row["state"] in TERMINAL,
            "scientific_convergence": "not_assessed",
        }

    def register(self, args: JobRegister) -> dict:
        identifier = stable_id(f"job:{args.workspace_id}:{args.key}")
        request = args.model_dump_json()
        with self.connect() as db:
            old = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if old and json.loads(old["request"]) != json.loads(request):
                raise ValueError("此 key 已登记不同作业；请读取原记录或使用新的 key")
            db.execute(
                "INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    str(args.workspace_id),
                    request,
                    "PENDING",
                    None,
                    time.time() + args.duration_seconds if args.backend == "simulated" else None,
                    None,
                    None,
                    time.time(),
                ),
            )
            return self.job(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())

    def jobs(self, workspace: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs"
                + (" WHERE workspace=?" if workspace else "")
                + " ORDER BY created",
                (workspace,) if workspace else (),
            ).fetchall()
            return [self.job(row) for row in rows]

    def observe(
        self, identifier: str, state: str, exit_code: str | None = None, error: str | None = None
    ) -> None:
        with self.connect() as db:
            old = db.execute("SELECT state FROM jobs WHERE id=?", (identifier,)).fetchone()
            if old is None:
                raise ValueError("作业不存在")
            if old["state"] not in TERMINAL:
                db.execute(
                    "UPDATE jobs SET state=?,exit_code=?,observed=?,error=? WHERE id=?",
                    (state, exit_code, time.time(), error, identifier),
                )
            self._reconcile(db)

    def tick_simulations(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.connect() as db:
            for row in db.execute("SELECT * FROM jobs WHERE due IS NOT NULL").fetchall():
                if row["state"] in TERMINAL:
                    continue
                outcome = json.loads(row["request"])["outcome"]
                ended = now >= row["due"]
                db.execute(
                    "UPDATE jobs SET state=?,exit_code=?,observed=? WHERE id=?",
                    (
                        outcome if ended else "RUNNING",
                        ("0:0" if outcome == "COMPLETED" else "1:0") if ended else None,
                        now,
                        row["id"],
                    ),
                )
            self._reconcile(db)

    def register_wait(self, args: WaitRegister, project_path: str | None = None) -> dict:
        job_ids = sorted({str(identifier) for identifier in args.job_ids})
        workspace, thread = str(args.workspace_id), str(args.thread_id)
        key = f"wait:{workspace}:{thread}:{args.mode}:{','.join(job_ids)}"
        identifier = stable_id(key + (":codex" if args.delivery == "codex" else ""))
        with self.connect() as db:
            for job_id in job_ids:
                if not db.execute(
                    "SELECT id FROM jobs WHERE id=? AND workspace=?", (job_id, workspace)
                ).fetchone():
                    raise ValueError("等待中的作业必须全部属于当前工作空间")
            db.execute(
                "INSERT OR IGNORE INTO waits VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    workspace,
                    thread,
                    json.dumps(job_ids),
                    args.mode,
                    "waiting",
                    time.time(),
                ),
            )
            if args.delivery == "codex":
                instruction = args.summary.strip()
                db.execute(
                    "INSERT OR IGNORE INTO wakeups VALUES(?,?,?)",
                    (identifier, instruction, project_path),
                )
                existing = db.execute(
                    "SELECT instruction FROM wakeups WHERE wait_id=?", (identifier,)
                ).fetchone()
                if existing["instruction"] != instruction:
                    raise ValueError("这组作业已有自动汇总登记，请沿用原有汇总要求")
            self._reconcile(db)
        return self.read_wait(workspace, identifier)

    def _reconcile(self, db: sqlite3.Connection) -> None:
        """Observation and outbox insertion commit atomically, including late subscribers."""
        for wait in db.execute("SELECT * FROM waits WHERE status='waiting'").fetchall():
            jobs = [
                self.job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                for job_id in json.loads(wait["jobs"])
            ]
            ready = (
                all(job["terminal"] for job in jobs)
                if wait["mode"] == "all"
                else any(job["terminal"] for job in jobs)
            )
            if not ready:
                continue
            payload = {
                "thread_id": wait["thread"],
                "workspace_id": wait["workspace"],
                "jobs": jobs,
                "kind": "jobs.terminal",
                "scientific_convergence": "not_assessed",
            }
            db.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)",
                (
                    stable_id(f"event:{wait['id']}"),
                    wait["id"],
                    json.dumps(payload),
                    None,
                    time.time(),
                ),
            )
            db.execute("UPDATE waits SET status='ready' WHERE id=?", (wait["id"],))

    def read_wait(self, workspace: str, identifier: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM waits WHERE id=? AND workspace=?", (identifier, workspace)
            ).fetchone()
            if row is None:
                raise ValueError("工作空间中不存在此等待")
            event = db.execute("SELECT * FROM events WHERE wait_id=?", (identifier,)).fetchone()
            wakeup = db.execute("SELECT 1 FROM wakeups WHERE wait_id=?", (identifier,)).fetchone()
            result = {
                "id": identifier,
                "thread_id": row["thread"],
                "status": row["status"],
                "event": (
                    {
                        "id": event["id"],
                        "data": json.loads(event["payload"]),
                        "acknowledged": event["acknowledged"] is not None,
                    }
                    if event
                    else None
                ),
                "delivery": "codex" if wakeup else "pull",
                "automatic_wakeup": "enabled" if wakeup else "unavailable",
            }
            if wakeup:
                result.update(
                    self.delivery_status(db, event["id"] if event else None, row["status"])
                )
            return result

    @staticmethod
    def delivery_status(db: sqlite3.Connection, event_id: str | None, wait_status: str) -> dict:
        row = (
            db.execute(
                "SELECT status AS delivery_status, turn_id, error AS delivery_error "
                "FROM deliveries WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if event_id
            else None
        )
        return (
            dict(row) if row else {"delivery_status": MonitorLedger.initial_delivery(wait_status)}
        )

    @staticmethod
    def initial_delivery(wait_status: str) -> str:
        return {
            "waiting": "waiting_for_job",
            "ready": "pending",
            "acknowledged": "already_acknowledged",
        }.get(wait_status, wait_status)

    def list_waits(self, workspace: str, offset: int) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT w.id,w.thread AS thread_id,w.jobs,w.mode,w.status,"
                "u.wait_id AS wakeup,e.id AS event_id,d.status AS delivery_status,d.turn_id,"
                "d.error AS delivery_error FROM waits w LEFT JOIN wakeups u ON u.wait_id=w.id "
                "LEFT JOIN events e ON e.wait_id=w.id LEFT JOIN deliveries d ON d.event_id=e.id "
                "WHERE w.workspace=? ORDER BY w.created DESC,w.id LIMIT 101 OFFSET ?",
                (workspace, offset),
            ).fetchall()
        waits = []
        for row in rows[:100]:
            wait = dict(row)
            wait["job_ids"] = json.loads(wait.pop("jobs"))
            wait["delivery"] = "codex" if wait.pop("wakeup") else "pull"
            if wait["delivery"] == "codex" and wait["delivery_status"] is None:
                wait["delivery_status"] = self.initial_delivery(wait["status"])
            waits.append(wait)
        return {"waits": waits, "next_offset": offset + 100 if len(rows) > 100 else None}

    def wait_status(self, wait_id: str) -> str:
        with self.connect() as db:
            return db.execute("SELECT status FROM waits WHERE id=?", (wait_id,)).fetchone()[
                "status"
            ]

    def control_wait(self, args: WaitControl) -> dict:
        workspace, identifier = str(args.workspace_id), str(args.wait_id)
        with self.connect() as db:
            wait = db.execute(
                "SELECT * FROM waits WHERE id=? AND workspace=?", (identifier, workspace)
            ).fetchone()
            if wait is None:
                raise ValueError("工作空间中不存在此等待")
            if args.action == "cancel":
                if wait["status"] != "acknowledged":
                    db.execute("UPDATE waits SET status='cancelled' WHERE id=?", (identifier,))
            else:
                if wait["status"] == "cancelled":
                    raise ValueError("等待已取消")
                event = db.execute(
                    "SELECT id FROM events WHERE wait_id=? AND id=?",
                    (identifier, str(args.event_id)),
                ).fetchone()
                if event is None:
                    raise ValueError("请读取事件并使用准确的 event_id 确认")
                db.execute(
                    "UPDATE events SET acknowledged=COALESCE(acknowledged,?) WHERE id=?",
                    (time.time(), event["id"]),
                )
                db.execute("UPDATE waits SET status='acknowledged' WHERE id=?", (identifier,))
        return self.read_wait(workspace, identifier)
