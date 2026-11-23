"""Exercise the registration API and durable delivery across process loss."""

from collections import defaultdict
from pathlib import Path
from uuid import uuid4

import pytest

from chemistry_agent.research_plugins.dispatcher import Dispatcher
from chemistry_agent.research_plugins.service import ResearchService


class QueueServer:
    def __init__(self) -> None:
        self.resumed = set()
        self.busy = set()
        self.queued = defaultdict(list)
        self.turns = defaultdict(dict)
        self.enqueues = []
        self.starts = []
        self.lose_reply = None

    def disconnect_after(self, phase: str) -> None:
        if self.lose_reply == phase:
            self.lose_reply = None
            raise OSError("response lost after write")

    def find_turn(self, thread_id, message_id, marker, since, turn_id):
        return self.turns[thread_id].get(message_id)

    def queue(self, thread_id):
        return self.queued[thread_id].copy()

    def thread(self, thread_id):
        return {"status": {"type": "active" if thread_id in self.busy else "idle"}}

    def resume(self, thread_id):
        self.resumed.add(thread_id)
        return self.thread(thread_id)

    def enqueue(self, thread_id, message_id, prompt):
        item = {"id": str(uuid4()), "clientUserMessageId": message_id, "input": prompt}
        self.queued[thread_id].append(item)
        self.enqueues.append((thread_id, item))
        self.disconnect_after("enqueue")
        return item

    def start(self, thread_id, submission_id):
        item = self.queued[thread_id].pop(0)
        assert item["id"] == submission_id and thread_id not in self.busy
        self.starts.append(thread_id)
        turn = {"id": str(uuid4()), "status": "inProgress"}
        self.turns[thread_id][item["clientUserMessageId"]] = turn
        self.busy.add(thread_id)
        self.disconnect_after("start")
        return turn

    def delete(self, thread_id, submission_id):
        self.queued[thread_id] = [i for i in self.queued[thread_id] if i["id"] != submission_id]

    def unsubscribe(self, thread_id):
        self.resumed.discard(thread_id)

    def close(self):
        self.resumed.clear()


async def arrange_group(service, folder: Path, workspace: str, **options):
    scope = {"workspace_id": workspace}
    await service.execute("workspace_open", scope | {"project_path": str(folder)})
    jobs = [
        await service.execute("monitor_register_job", scope | {"key": key, "name": key})
        for key in ("model-A", "model-B")
    ]
    args = scope | {
        "job_ids": [job["id"] for job in jobs],
        "thread_id": str(uuid4()),
        **options,
    }
    group = await service.execute("monitor_register_wait", args)
    return group, args


def delivery(service, group, args):
    return service.ledger.read_wait(args["workspace_id"], group["id"])


async def test_default_two_groups_original_chats_and_restarts(tmp_path):
    service = ResearchService(tmp_path, project_roots=(tmp_path,), codex_wakeup=True)
    first, a = await arrange_group(service, tmp_path, str(uuid4()), mode="all", summary="Compare A")
    second, b = await arrange_group(service, tmp_path, str(uuid4()), mode="any", summary="Check B")
    pull = await service.execute("monitor_register_wait", b | {"summary": "", "delivery": "pull"})
    assert first["delivery"] == second["delivery"] == "codex" and pull["delivery"] == "pull"
    assert first == await service.execute("monitor_register_wait", a)
    with pytest.raises(ValueError, match="汇总要求"):
        await service.execute("monitor_register_wait", a | {"summary": "changed"})
    server = QueueServer()
    connections = []

    def connect():
        connections.append(1)
        return server

    Dispatcher(service.ledger, connect).tick()
    assert not connections
    service.ledger.observe(a["job_ids"][0], "COMPLETED", "0:0")
    service.ledger.observe(b["job_ids"][0], "FAILED", "1:0")
    server.busy.add(b["thread_id"])
    Dispatcher(service.ledger, connect).tick()
    assert delivery(service, first, a)["status"] == "waiting"
    assert delivery(service, second, b)["delivery_status"] == "waiting_for_idle"
    service.ledger.observe(a["job_ids"][1], "COMPLETED", "0:0")
    Dispatcher(service.ledger, connect).tick()
    assert [thread for thread, _ in server.enqueues] == [a["thread_id"]]
    server.busy.clear()
    restarted = ResearchService(tmp_path, project_roots=(tmp_path,), codex_wakeup=True)
    worker = Dispatcher(restarted.ledger, connect)
    worker.tick()
    worker.tick()
    for turn_map in server.turns.values():
        for turn in turn_map.values():
            turn["status"] = "completed"
    worker.tick()
    Dispatcher(restarted.ledger, connect).tick()
    assert server.starts == [a["thread_id"], b["thread_id"]]
    assert len(server.enqueues) == 2
    assert "Compare A" in server.enqueues[0][1]["input"]
    assert "Check B" in server.enqueues[1][1]["input"]
    assert "PENDING" in server.enqueues[1][1]["input"]
    assert delivery(service, first, a)["delivery_status"] == "completed"
    assert delivery(service, second, b)["delivery_status"] == "completed"
    waits = await restarted.execute("monitor_list_waits", {"workspace_id": b["workspace_id"]})
    assert {wait["id"] for wait in waits["waits"]} == {second["id"], pull["id"]}


@pytest.mark.parametrize("phase", ["enqueue", "start"])
async def test_lost_reply_reconciles_without_duplicate(tmp_path, phase):
    service = ResearchService(tmp_path, project_roots=(tmp_path,), codex_wakeup=True)
    wait, args = await arrange_group(service, tmp_path, str(uuid4()), mode="any")
    service.ledger.observe(args["job_ids"][0], "COMPLETED", "0:0")
    server = QueueServer()
    server.lose_reply = phase
    worker = Dispatcher(service.ledger, lambda: server)
    worker.tick()
    if phase == "start":
        worker.tick()
    worker.close()
    worker = Dispatcher(service.ledger, lambda: server)
    worker.tick()
    worker.tick()
    assert len(server.enqueues) == len(server.starts) == 1
    assert delivery(service, wait, args)["delivery_status"] == "accepted"


async def test_cancel_and_user_deleted_queue_message(tmp_path):
    service = ResearchService(tmp_path, project_roots=(tmp_path,), codex_wakeup=True)
    wait, args = await arrange_group(service, tmp_path, str(uuid4()), mode="any")
    second, other = await arrange_group(service, tmp_path, str(uuid4()), mode="any")
    for group in (args, other):
        service.ledger.observe(group["job_ids"][0], "COMPLETED", "0:0")
    server = QueueServer()
    worker = Dispatcher(service.ledger, lambda: server)
    worker.tick()
    await service.execute(
        "monitor_control_wait",
        {
            "workspace_id": args["workspace_id"],
            "wait_id": wait["id"],
            "action": "cancel",
        },
    )
    server.queued[other["thread_id"]].clear()
    worker.tick()
    worker.tick()
    assert len(server.enqueues) == 2 and not server.starts
    assert not server.queued[args["thread_id"]]
    assert delivery(service, wait, args)["delivery_status"] == "cancelled"
    assert delivery(service, second, other)["delivery_status"] == "uncertain"
