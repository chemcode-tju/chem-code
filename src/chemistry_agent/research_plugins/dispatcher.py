"""Deliver opted-in job events to their original Codex chats."""

import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from uuid import NAMESPACE_URL, uuid5

from chemistry_agent.research_plugins.app_server_client import AppServerClient, RpcError
from chemistry_agent.research_plugins.ledger import MonitorLedger

logger = logging.getLogger(__name__)
UNSENT = ("pending", "waiting_for_idle", "waiting_for_queue")
FINISHED = ("completed", "failed", "interrupted", "cancelled", "already_acknowledged")


def wakeup_message(row: dict) -> str:
    """Pass the recorded snapshot and analysis scope, like Chem Code's event adapter."""
    event = json.loads(row["payload"])
    body = {
        "workspace_id": row["workspace"],
        "wait_id": row["wait_id"],
        "event_id": row["event_id"],
        "mode": row["mode"],
        "project_path": row["project_path"],
        "summary": row["instruction"] or "核对已结束作业的输出，汇总结果、失败和缺失项。",
        "jobs": [
            {
                key: job[key]
                for key in (
                    "id",
                    "name",
                    "backend",
                    "host",
                    "scheduler_id",
                    "scheduler_state",
                    "exit_code",
                    "terminal",
                    "plan_id",
                    "node_id",
                )
                if key in job
            }
            for job in event["jobs"]
        ],
    }
    return (
        f"[chem-monitor-event:{row['event_id']}]\n"
        "Chem Code 后台监控通知。按照已登记要求，只读核实现有结果并在本聊天汇总。"
        "禁止提交、重试或取消作业，禁止修改科研文件和发起下游计算。"
        "all 表示全部作业已到终态，any 表示至少一个到终态。"
        "仍在运行的作业只记录其状态。区分调度终态、退出码、输出完整性和科学收敛。"
        "可用 monitor_read_wait 读取事件详情，必要时查关联方案步骤。"
        "汇总后用 monitor_control_wait 确认此 wait_id 和 event_id。\n"
        + json.dumps(body, ensure_ascii=False)
    )


class Dispatcher:
    def __init__(self, ledger: MonitorLedger, connect: Callable[[], AppServerClient]) -> None:
        self.ledger = ledger
        self.connect = connect
        self.api: AppServerClient | None = None

    def candidates(self) -> list[dict]:
        with self.ledger.connect() as db:
            rows = db.execute(
                "SELECT e.id AS event_id,e.payload,e.wait_id,w.workspace,w.thread,w.mode,"
                "w.status AS wait_status,u.instruction,u.project_path,"
                "d.prompt,d.message_id,d.status,d.submission_id,d.turn_id,d.created,d.error "
                "FROM waits w JOIN wakeups u ON u.wait_id=w.id JOIN events e ON e.wait_id=w.id "
                "LEFT JOIN deliveries d ON d.event_id=e.id "
                "WHERE (w.status='ready' OR d.event_id IS NOT NULL) "
                "AND (d.status IS NULL OR d.status NOT IN (?,?,?,?,?)) "
                "ORDER BY COALESCE(d.updated,0),e.created,e.id LIMIT 32",
                FINISHED,
            ).fetchall()
            return [dict(row) for row in rows]

    def prepare(self, row: dict) -> None:
        if row["status"] is not None:
            return
        now = time.time()
        row.update(
            status="pending",
            created=now,
            prompt=wakeup_message(row),
            message_id=str(uuid5(NAMESPACE_URL, f"chem-monitor-trigger:{row['event_id']}")),
        )
        with self.ledger.connect() as db:
            db.execute(
                "INSERT INTO deliveries(event_id,thread_id,message_id,prompt,status,"
                "created,updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    row["event_id"],
                    row["thread"],
                    row["message_id"],
                    row["prompt"],
                    "pending",
                    now,
                    now,
                ),
            )

    def record(
        self,
        row: dict,
        status: str,
        *,
        error: str | None = None,
        submission_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        with self.ledger.connect() as db:
            db.execute(
                "UPDATE deliveries SET status=?,error=?,updated=?,"
                "submission_id=COALESCE(?,submission_id),turn_id=COALESCE(?,turn_id) "
                "WHERE event_id=?",
                (status, error, time.time(), submission_id, turn_id, row["event_id"]),
            )
        if (status, error) != (row["status"], row["error"]):
            logger.info("event=%s thread=%s status=%s", row["event_id"], row["thread"], status)
        row.update(status=status, error=error)

    def wait_status(self, row: dict) -> str:
        return self.ledger.wait_status(row["wait_id"])

    def advance(self, api: AppServerClient, row: dict) -> None:
        event_id, thread_id = row["event_id"], row["thread"]
        wait_status = self.wait_status(row)
        if row["status"] not in UNSENT:
            turn = api.find_turn(
                thread_id,
                row["message_id"],
                f"[chem-monitor-event:{event_id}]",
                row["created"],
                row["turn_id"],
            )
            if turn:
                status = "accepted" if turn["status"] == "inProgress" else turn["status"]
                self.record(row, status, turn_id=turn["id"])
                if status == "accepted" and thread_id not in api.resumed:
                    api.resume(thread_id)
                return
        queue = api.queue(thread_id)
        own = next(
            (item for item in queue if item["clientUserMessageId"] == row["message_id"]), None
        )
        if wait_status != "ready":
            if own:
                api.delete(thread_id, own["id"])
            self.record(row, wait_status if wait_status == "cancelled" else "already_acknowledged")
            return
        if own is None and row["status"] not in UNSENT:
            self.record(row, "uncertain", error="队列和历史中暂未找到回执，不重复投递")
            return
        if api.thread(thread_id)["status"]["type"] == "active":
            self.record(row, "queued" if own else "waiting_for_idle")
            return
        if queue and (own is None or own["id"] != queue[0]["id"]):
            self.record(row, "queued" if own else "waiting_for_queue")
            return
        thread = api.resume(thread_id)
        if thread["status"]["type"] != "idle":
            self.record(row, "queued" if own else "waiting_for_idle")
            return
        if self.wait_status(row) != "ready":
            return
        if own is None:
            self.record(row, "queuing")
            own = api.enqueue(thread_id, row["message_id"], row["prompt"])
            self.record(row, "queued", submission_id=own["id"])
            return
        self.record(row, "starting", submission_id=own["id"])
        turn = api.start(thread_id, own["id"])
        status = "accepted" if turn["status"] == "inProgress" else turn["status"]
        self.record(row, status, turn_id=turn["id"])

    def tick(self) -> None:
        for row in self.candidates():
            self.prepare(row)
            if row["status"] in UNSENT and self.wait_status(row) != "ready":
                status = self.wait_status(row)
                self.record(row, status if status == "cancelled" else "already_acknowledged")
                continue
            try:
                if self.api is None:
                    self.api = self.connect()
                self.advance(self.api, row)
                if row["status"] in FINISHED and row["thread"] in self.api.resumed:
                    self.api.unsubscribe(row["thread"])
            except RpcError as error:
                self.record(row, row["status"], error=str(error)[:600])
            except Exception as error:
                # A lost response may hide a successful write, so preserve the pre-send phase.
                self.record(row, row["status"], error=str(error)[:600])
                self.close()
                break

    def close(self) -> None:
        if self.api is not None:
            with suppress(Exception):
                self.api.close()
            self.api = None
