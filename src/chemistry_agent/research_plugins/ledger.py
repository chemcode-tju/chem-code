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

    def register_wait(self, args: WaitRegister) -> dict:
        job_ids = sorted({str(identifier) for identifier in args.job_ids})
        workspace, thread = str(args.workspace_id), str(args.thread_id)
        identifier = stable_id(f"wait:{workspace}:{thread}:{args.mode}:{','.join(job_ids)}")
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
            return {
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
                "delivery": "pull",
                "automatic_wakeup": "unavailable",
            }

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
