"""Durable, account-scoped copies of readable Slurm allocations for the dashboard."""

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing, suppress
from datetime import date, datetime, timedelta

from chemistry_agent.automation.queue_remote import (
    TERMINAL_STATES,
    board_start,
    calendar_dates,
    dashboard_view,
)
from chemistry_agent.errors import AgentError

logger = logging.getLogger(__name__)
SYNC_SECONDS = 6 * 60 * 60
RETRY_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS syncs (
    host TEXT PRIMARY KEY, account TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    host TEXT NOT NULL, account TEXT NOT NULL, job_id TEXT NOT NULL, submitted TEXT NOT NULL,
    ended TEXT NOT NULL, rank INTEGER NOT NULL, observed REAL NOT NULL,
    inferred_gpu TEXT, payload TEXT NOT NULL,
    PRIMARY KEY (host, account, job_id, submitted)
);
CREATE INDEX IF NOT EXISTS job_end ON jobs (host, account, ended);
CREATE TABLE IF NOT EXISTS covered_days (
    host TEXT NOT NULL, account TEXT NOT NULL, day TEXT NOT NULL,
    PRIMARY KEY (host, account, day)
);
"""
UPSERT_JOB = """
INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (host, account, job_id, submitted) DO UPDATE SET
    ended = excluded.ended, rank = excluded.rank, observed = excluded.observed,
    inferred_gpu = coalesce(jobs.inferred_gpu, excluded.inferred_gpu),
    payload = excluded.payload
WHERE excluded.rank > jobs.rank
   OR (excluded.rank = jobs.rank AND excluded.observed >= jobs.observed)
"""


def job_key(job: dict) -> tuple[str, str]:
    return job["id"], job["submitted"]


def job_rank(job: dict) -> int:
    if job["status"] in TERMINAL_STATES:
        return 2
    return int(bool(job["resources"] or job["cpu_seconds"]))


class JobHistory:
    def __init__(self, scheduler) -> None:
        self.scheduler = scheduler
        settings = scheduler.settings
        self.path = settings.slurm_history_path or settings.platform_path / "slurm-history.sqlite3"
        self.hosts = tuple(
            dict.fromkeys(
                host.strip() for host in settings.slurm_history_hosts.split(",") if host.strip()
            )
        )
        self.worker: asyncio.Task | None = None

    def start(self) -> None:
        if self.hosts and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.watch())

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker

    async def watch(self) -> None:
        while True:
            succeeded = True
            available = set(self.scheduler.hosts())
            for host in self.hosts:
                if host not in available:
                    continue
                try:
                    snapshot = await self.snapshot(host, "all")
                    if snapshot["accounting_available"]:
                        await asyncio.to_thread(self.save, host, snapshot)
                    else:
                        succeeded = False
                        logger.warning(
                            "Slurm accounting unavailable for history collection on %s", host
                        )
                except Exception as exc:
                    succeeded = False
                    logger.warning(
                        "Slurm history collection failed on %s (%s)", host, type(exc).__name__
                    )
            await asyncio.sleep(SYNC_SECONDS if succeeded else RETRY_SECONDS)

    async def snapshot(self, host: str, period: str) -> dict:
        return await self.scheduler.request(
            host, {"action": "queue_dashboard_snapshot", "period": period}
        )

    def save(self, host: str, snapshot: dict) -> None:
        if not snapshot["accounting_available"]:
            return
        account = snapshot["account"]
        observed = datetime.fromisoformat(snapshot["observed_at"])
        first = snapshot["coverage_start"]
        # A partial current day is retained as records, and confirmed by a later collection.
        last_closed = observed.date() - timedelta(days=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        creation = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(creation)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)
            with db:
                db.executemany(
                    UPSERT_JOB,
                    (
                        (
                            host,
                            account,
                            *job_key(job),
                            job["ended"],
                            job_rank(job),
                            observed.timestamp(),
                            job["inferred_gpu"],
                            json.dumps(job),
                        )
                        for job in snapshot["records"]
                    ),
                )
                if first:
                    db.executemany(
                        "INSERT OR IGNORE INTO covered_days VALUES (?, ?, ?)",
                        (
                            (host, account, day.isoformat())
                            for day in calendar_dates(date.fromisoformat(first), last_closed)
                        ),
                    )
                db.execute(
                    "INSERT INTO syncs VALUES (?, ?, ?) ON CONFLICT (host) DO UPDATE SET "
                    "account = excluded.account, observed_at = excluded.observed_at "
                    "WHERE julianday(excluded.observed_at) >= julianday(syncs.observed_at)",
                    (host, account, snapshot["observed_at"]),
                )

    def read(self, host: str, live: dict | None, period: str) -> tuple[dict, set[str], str] | None:
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path)) as db:
            # Backfill can create the database just before this request reads it.
            db.executescript(SCHEMA)
            db.execute("BEGIN")
            synced = db.execute(
                "SELECT account, observed_at FROM syncs WHERE host = ?", (host,)
            ).fetchone()
            if not synced or (live and synced[0] != live["account"]):
                return None
            account, last_sync = synced
            observed = (
                datetime.fromisoformat(live["observed_at"])
                if live
                else datetime.now(datetime.fromisoformat(last_sync).tzinfo)
            )
            start = board_start(observed.date(), period)
            cutoff = start.isoformat() if start else ""
            records = {}
            for payload, model in db.execute(
                "SELECT payload, inferred_gpu FROM jobs WHERE host = ? AND account = ? "
                "AND (submitted >= ? OR ended >= ?)",
                (host, account, cutoff, cutoff),
            ):
                job = json.loads(payload)
                job["inferred_gpu"] = model
                records[job_key(job)] = job
            days = {
                day
                for (day,) in db.execute(
                    "SELECT day FROM covered_days WHERE host = ? AND account = ? AND day >= ?",
                    (host, account, cutoff),
                )
            }
        for job in (live or {}).get("records", ()):
            previous = records.get(job_key(job))
            if previous is None or job_rank(job) >= job_rank(previous):
                records[job_key(job)] = job | {
                    "inferred_gpu": (previous or {}).get("inferred_gpu") or job["inferred_gpu"],
                }
        snapshot = live or {
            "account": account,
            "active": None,
            "accounting_available": False,
            "gpu_tracked": False,
            "coverage_start": None,
            "observed_at": observed.isoformat(timespec="seconds"),
        }
        return snapshot | {"records": list(records.values())}, days, last_sync

    async def dashboard(self, host: str, period: str) -> dict:
        if host not in self.hosts or host not in self.scheduler.hosts():
            return await self.scheduler.request(
                host, {"action": "queue_dashboard", "period": period}
            )
        try:
            snapshot = await self.snapshot(host, period)
        except AgentError:
            retained = await asyncio.to_thread(self.read, host, None, period)
            if retained is None:
                raise
        else:
            retained = await asyncio.to_thread(self.read, host, snapshot, period)
            if retained is None:
                return dashboard_view(snapshot, period)
        combined, days, last_sync = retained
        return dashboard_view(combined, period, days) | {
            "history_synced_at": last_sync,
            "live_accounting_available": combined["accounting_available"],
        }
