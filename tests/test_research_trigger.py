"""Failure injection for a single authorized completion, without model or cluster calls."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from chemistry_agent.research_plugins.ledger import MonitorLedger
from chemistry_agent.research_plugins.models import JobRegister, WaitControl, WaitRegister
from chemistry_agent.research_plugins.trigger import OneShotTrigger, TriggerJob


class FakeControl:
    def __init__(self) -> None:
        self.resumed = set()
        self.busy = False
        self.queued = []
        self.turn = None
        self.enqueues = 0
        self.starts = 0
        self.lose_enqueue_reply = False
        self.lose_start_reply = False

    def find_turn(self, *args):
        return self.turn

    def queue(self, thread_id):
        return self.queued.copy()

    def thread(self, thread_id):
        return {"status": {"type": "active" if self.busy else "idle"}}

    def resume(self, thread_id):
        self.resumed.add(thread_id)
        return self.thread(thread_id)

    def enqueue(self, thread_id, message_id, prompt):
        self.enqueues += 1
        item = {"id": str(uuid4()), "clientUserMessageId": message_id, "input": prompt}
        self.queued.append(item)
        if self.lose_enqueue_reply:
            raise OSError("reply lost after server persisted queue")
        return item

    def start(self, thread_id, submission_id):
        assert self.queued[0]["id"] == submission_id
        self.starts += 1
        self.queued.pop(0)
        self.turn = {"id": str(uuid4()), "status": "inProgress"}
        if self.lose_start_reply:
            raise OSError("reply lost after turn started")
        return self.turn


@pytest.fixture
def target(tmp_path):
    workspace, thread = str(uuid4()), str(uuid4())
    ledger = MonitorLedger(tmp_path / "monitor.sqlite3")
    job = ledger.register(
        JobRegister(
            workspace_id=workspace,
            key="1748",
            name="test",
            backend="slurm",
            host="cluster-a",
            scheduler_id="1748",
        )
    )
    wait = ledger.register_wait(
        WaitRegister(
            workspace_id=workspace,
            thread_id=thread,
            job_ids=[job["id"]],
        )
    )
    trigger = OneShotTrigger(
        ledger.path,
        tmp_path / "deliveries.sqlite3",
        workspace,
        wait["id"],
        thread,
        (TriggerJob(job["id"], "1748", Path("/authorized/run_1748"), "test"),),
    )
    return trigger, ledger


def ready(target):
    trigger, ledger = target
    ledger.observe(trigger.jobs[0].job_id, "COMPLETED", "0:0")
    return trigger, trigger.event()


def test_running_job_never_opens_app_server(target, monkeypatch):
    trigger, _ = target
    assert trigger.event() is None

    def stop(_):
        raise InterruptedError

    def forbidden():
        pytest.fail("A running job must not connect to Codex")

    monkeypatch.setattr("chemistry_agent.research_plugins.trigger.time.sleep", stop)
    with pytest.raises(InterruptedError):
        trigger.run(forbidden)


def test_one_event_one_turn_and_restart_deduplication(target):
    trigger, event = ready(target)
    api = FakeControl()
    assert trigger.advance(api, event) == "queued"
    assert trigger.advance(api, event) == "accepted"
    assert trigger.advance(api, event) == "accepted"
    api.turn["status"] = "completed"
    assert trigger.advance(api, event) == "completed"
    # Restarted process reuses the persisted delivery record and never sends again.
    trigger.journal = type(trigger.journal)(trigger.journal.path)
    assert trigger.advance(api, event) == "completed"
    assert (api.enqueues, api.starts) == (1, 1)


def test_busy_chat_and_existing_queue_are_respected(target):
    trigger, event = ready(target)
    api = FakeControl()
    api.busy = True
    assert trigger.advance(api, event) == "waiting_for_idle"
    assert api.enqueues == 0
    api.busy = False
    api.queued = [{"id": "user-message", "clientUserMessageId": "user"}]
    assert trigger.advance(api, event) == "waiting_for_queue"
    assert api.enqueues == 0
    api.queued.clear()
    assert trigger.advance(api, event) == "queued"
    api.busy = True
    assert trigger.advance(api, event) == "waiting_for_idle"
    assert api.starts == 0


@pytest.mark.parametrize("phase", ["enqueue", "start"])
def test_lost_response_reconciles_without_repeating_side_effect(target, phase):
    trigger, event = ready(target)
    api = FakeControl()
    if phase == "enqueue":
        api.lose_enqueue_reply = True
    else:
        trigger.advance(api, event)
        api.lose_start_reply = True
    with pytest.raises(OSError):
        trigger.advance(api, event)
    api.lose_enqueue_reply = api.lose_start_reply = False
    trigger.advance(api, event)
    assert trigger.advance(api, event) == "accepted"
    assert (api.enqueues, api.starts) == (1, 1)


def test_crash_before_send_is_uncertain_not_a_blind_retry(target):
    trigger, event = ready(target)
    trigger.journal.prepare(event["id"], trigger.thread_id, trigger.prompt(event["id"]))
    trigger.journal.update(event["id"], "queuing")
    api = FakeControl()
    assert trigger.advance(api, event) == "uncertain"
    assert trigger.advance(api, event) == "uncertain"
    assert api.enqueues == api.starts == 0


def test_user_deleted_queued_notification_is_not_recreated(target):
    trigger, event = ready(target)
    api = FakeControl()
    trigger.advance(api, event)
    api.queued.clear()
    assert trigger.advance(api, event) == "uncertain"
    assert (api.enqueues, api.starts) == (1, 0)


def test_cancelled_acknowledged_and_mismatched_targets_do_not_dispatch(target):
    trigger, ledger = target
    original = trigger.jobs
    trigger.jobs = (replace(original[0], scheduler_id="1749"),)
    ledger.observe(trigger.jobs[0].job_id, "FAILED", "1:0")
    with pytest.raises(ValueError, match="不匹配"):
        trigger.event()
    trigger.jobs = original
    event = trigger.event()
    ledger.control_wait(
        WaitControl(
            workspace_id=trigger.workspace_id,
            wait_id=trigger.wait_id,
            action="acknowledge",
            event_id=event["id"],
        )
    )
    api = FakeControl()
    assert trigger.advance(api, trigger.event()) == "already_acknowledged"
    assert api.enqueues == 0


def test_wait_cancellation_stops_trigger(target):
    trigger, ledger = target
    ledger.control_wait(
        WaitControl(
            workspace_id=trigger.workspace_id,
            wait_id=trigger.wait_id,
            action="cancel",
        )
    )
    assert trigger.event() == {"cancelled": True}


def test_two_jobs_wait_for_all_terminal_and_send_one_summary(target):
    trigger, ledger = target
    other = ledger.register(
        JobRegister(
            workspace_id=trigger.workspace_id,
            key="second",
            name="second",
            backend="slurm",
            host="cluster-a",
            scheduler_id="1756",
        )
    )
    trigger.jobs += (TriggerJob(other["id"], "1756", Path("/authorized/ase_1756"), "ASE"),)
    wait = ledger.register_wait(
        WaitRegister(
            workspace_id=trigger.workspace_id,
            thread_id=trigger.thread_id,
            job_ids=[job.job_id for job in trigger.jobs],
            mode="all",
        )
    )
    trigger.wait_id = wait["id"]
    ledger.observe(trigger.jobs[0].job_id, "COMPLETED", "0:0")
    assert trigger.event() is None
    ledger.observe(other["id"], "FAILED", "1:0")
    event = trigger.event()
    api = FakeControl()
    assert trigger.advance(api, event) == "queued"
    assert "/authorized/ase_1756" in api.queued[0]["input"]
    assert trigger.advance(api, event) == "accepted"
    assert (api.enqueues, api.starts) == (1, 1)
    with ledger.connect() as db:
        db.execute("UPDATE waits SET mode='any' WHERE id=?", (trigger.wait_id,))
    with pytest.raises(ValueError, match="不匹配"):
        trigger.event()
