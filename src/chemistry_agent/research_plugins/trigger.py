"""One explicitly authorized wait -> one queued message in its original Codex chat."""

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from chemistry_agent.research_plugins.app_server_client import AppServerClient
from chemistry_agent.research_plugins.ledger import TERMINAL

logger = logging.getLogger(__name__)
FINISHED = {"completed", "failed", "interrupted", "cancelled", "already_acknowledged"}


@dataclass(frozen=True)
class TriggerJob:
    job_id: str
    scheduler_id: str
    result_directory: Path
    label: str


class TriggerJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                event_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, message_id TEXT NOT NULL,
                prompt TEXT NOT NULL, status TEXT NOT NULL, submission_id TEXT, turn_id TEXT,
                created REAL NOT NULL, updated REAL NOT NULL, error TEXT)""")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def prepare(self, event_id: str, thread_id: str, prompt: str) -> dict:
        with self.connect() as db:
            message_id = str(uuid5(NAMESPACE_URL, f"chem-monitor-trigger:{event_id}"))
            db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    thread_id,
                    message_id,
                    prompt,
                    "pending",
                    None,
                    None,
                    time.time(),
                    time.time(),
                    None,
                ),
            )
            row = dict(
                db.execute("SELECT * FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
            )
            if row["thread_id"] != thread_id or row["prompt"] != prompt:
                raise ValueError("此事件已绑定另一份投递内容；拒绝重新投递")
            return row

    def update(
        self, event_id: str, status: str, expected: str | None = None, **fields: str | None
    ) -> bool:
        if fields.keys() - {"submission_id", "turn_id", "error"}:
            raise ValueError("Unknown delivery field")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE deliveries SET status=?,updated=?"
                + "".join(f",{name}=?" for name in fields)
                + " WHERE event_id=?"
                + (" AND status=?" if expected else ""),
                (
                    status,
                    time.time(),
                    *fields.values(),
                    event_id,
                    *((expected,) if expected else ()),
                ),
            )
            return changed.rowcount == 1

    def get(self, event_id: str) -> dict:
        with self.connect() as db:
            return dict(
                db.execute("SELECT * FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
            )


class OneShotTrigger:
    def __init__(
        self,
        monitor_path: Path,
        journal_path: Path,
        workspace_id: str,
        wait_id: str,
        thread_id: str,
        jobs: tuple[TriggerJob, ...],
    ) -> None:
        self.monitor_path, self.journal = monitor_path, TriggerJournal(journal_path)
        self.workspace_id, self.wait_id, self.thread_id = workspace_id, wait_id, thread_id
        if not jobs or len({job.job_id for job in jobs}) != len(jobs):
            raise ValueError("必须明确指定非空且不重复的作业集合")
        self.jobs = jobs

    def event(self) -> dict | None:
        """Read only the pinned wait; never scan or trigger other users' jobs/chats."""
        with closing(sqlite3.connect(f"file:{self.monitor_path}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            wait = db.execute("SELECT * FROM waits WHERE id=?", (self.wait_id,)).fetchone()
            if wait is None or (
                wait["workspace"] != self.workspace_id
                or wait["thread"] != self.thread_id
                or sorted(json.loads(wait["jobs"])) != sorted(job.job_id for job in self.jobs)
                or wait["mode"] != "all"
            ):
                raise ValueError("等待记录与授权的作业/原聊天不匹配")
            if wait["status"] == "cancelled":
                return {"cancelled": True}
            row = db.execute("SELECT * FROM events WHERE wait_id=?", (self.wait_id,)).fetchone()
            if row is None:
                return None
            event = dict(row)
            payload = json.loads(row["payload"])
            jobs = payload.get("jobs", [])
            expected = {job.job_id: job.scheduler_id for job in self.jobs}
            if (
                payload.get("thread_id") != self.thread_id
                or payload.get("workspace_id") != self.workspace_id
                or payload.get("kind") != "jobs.terminal"
                or len(jobs) != len(expected)
                or {job.get("id") for job in jobs} != expected.keys()
                or any(
                    job.get("host") != "cluster-a"
                    or job.get("scheduler_id") != expected.get(job.get("id"))
                    or job.get("scheduler_state") not in TERMINAL
                    for job in jobs
                )
            ):
                raise ValueError("事件内容与授权的终态作业不匹配")
            return event

    def prompt(self, event_id: str) -> str:
        job_details = "\n".join(
            f"- Slurm {job.scheduler_id}（{job.label}）：{job.result_directory}"
            for job in self.jobs
        )
        return f"""[chem-monitor-event:{event_id}]
用户已授权：以下 cluster-a Slurm 作业全部到达终态后，在本聊天自动处理一次结果。
这是持久监控器的完成通知，请不要重复轮询或重新提交计算。
工作空间：{self.workspace_id}
等待记录：{self.wait_id}
事件 ID：{event_id}
作业及运行目录：
{job_details}
请逐个核对 Slurm 终态、退出码和各目录 result.json。
成功时汇总两种跑法的配对稳态速度、速度比和实际完成步数，
把预热/启动开销与稳态计时分开，并核对两种跑法是否使用相同条件。
失败或结果缺失时报告失败位置、缺失项和已有结果，不能把调度结束当成测试成功。
仅分析现有文件并回复本聊天，不续跑、不改动模拟输入、不提交或取消其他作业。
报告完成后，使用 monitor_control_wait 确认上面的准确 event_id；不要确认其他事件。
"""

    def advance(self, api: AppServerClient, event: dict) -> str:
        """Reconcile before every send; uncertain delivery is never blindly retried."""
        event_id = event["id"]
        delivery = self.journal.prepare(event_id, self.thread_id, self.prompt(event_id))
        if delivery["status"] in FINISHED:
            return delivery["status"]
        if event["acknowledged"] is not None and delivery["status"] == "pending":
            self.journal.update(event_id, "already_acknowledged")
            return "already_acknowledged"
        marker = f"[chem-monitor-event:{event_id}]"
        turn = api.find_turn(
            self.thread_id, delivery["message_id"], marker, delivery["created"], delivery["turn_id"]
        )
        if turn:
            status = "accepted" if turn["status"] == "inProgress" else turn["status"]
            self.journal.update(event_id, status, turn_id=turn["id"], error=None)
            if status == "accepted" and self.thread_id not in api.resumed:
                api.resume(self.thread_id)
            return status
        queue = api.queue(self.thread_id)
        own = next(
            (item for item in queue if item["clientUserMessageId"] == delivery["message_id"]), None
        )
        if own:
            self.journal.update(event_id, "queued", submission_id=own["id"], error=None)
        elif delivery["status"] != "pending":
            # This also covers a user deleting our queued message: don't recreate it.
            self.journal.update(
                event_id, "uncertain", error="消息既不在队列也未出现在历史中；不自动重发"
            )
            return "uncertain"
        thread = api.thread(self.thread_id)
        if thread["status"]["type"] == "active":
            return "waiting_for_idle"
        if queue and (own is None or queue[0]["id"] != own["id"]):
            return "waiting_for_queue"
        thread = api.resume(self.thread_id)
        if thread["status"]["type"] != "idle":
            return "waiting_for_idle"
        if own is None:
            if not self.journal.update(event_id, "queuing", expected="pending"):
                return "reconciling"
            own = api.enqueue(self.thread_id, delivery["message_id"], delivery["prompt"])
            self.journal.update(event_id, "queued", submission_id=own["id"])
        # Read back on the next tick. The server may already have consumed the queue.
        if delivery["status"] == "pending":
            return "queued"
        self.journal.update(event_id, "starting")
        turn = api.start(self.thread_id, own["id"])
        status = "accepted" if turn["status"] == "inProgress" else turn["status"]
        self.journal.update(event_id, status, turn_id=turn["id"], error=None)
        return status

    def run(self, client_factory: Callable[[], AppServerClient], poll_seconds: float = 10) -> None:
        api, previous = None, None
        try:
            while True:
                event = self.event()
                if event and event.get("cancelled"):
                    logger.info("Wait cancelled; trigger stopped")
                    return
                status = "waiting_for_job"
                if event:
                    try:
                        if api is None:
                            api = client_factory()
                        status = self.advance(api, event)
                    except Exception as error:
                        # A pre-send journal entry survives any network/process failure.
                        logger.warning("Control connection will be reconciled: %s", error)
                        if api is not None:
                            with suppress(Exception):
                                api.close()
                        api = None
                        status = "reconnecting"
                if status != previous:
                    logger.info("trigger_status=%s", status)
                    previous = status
                if status in FINISHED:
                    return
                time.sleep(poll_seconds)
        finally:
            if api is not None:
                with suppress(Exception):
                    api.close()
