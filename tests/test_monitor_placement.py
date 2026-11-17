"""Observer migration/recovery operates on receipts, never replays scheduler submissions."""

import json
import os
import signal
import subprocess
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting import monitor_worker
from chemistry_agent.hosting.monitor import MonitorPlacement


@pytest.fixture
def placements(tmp_path, monkeypatch):
    saved, commands = [], []
    jobs = SimpleNamespace(
        root=tmp_path,
        settings=SimpleNamespace(allowed_ssh_hosts=()),
        chat=SimpleNamespace(plans=SimpleNamespace(store=None)),
        save=lambda session, record: saved.append(deepcopy(record)),
    )
    placement = MonitorPlacement(jobs)
    desired, scheduler = ["cluster-a"], {"status": "running", "terminal": False, "scheduler_job_id": "42"}
    monkeypatch.setattr(monitor_worker, "observe", lambda directory, watch: deepcopy(scheduler))
    monkeypatch.setattr(monitor_worker.subprocess, "Popen", lambda *a, **kw: commands.append(a))

    async def preference(session):
        return desired[0]

    async def call(host, record, generation, operation):
        return monitor_worker.control(
            {
                "directory": str(tmp_path / host),
                "key": record["id"],
                "generation": generation,
                "operation": operation,
                "request": placement.request(record),
                "worker": "fixture",
                "target": None,
            }
        )

    monkeypatch.setattr(placement, "preference", preference)
    monkeypatch.setattr(placement, "call", call)
    record = {
        "id": "test-job",
        "host": "cluster-b",
        "remote_directory": "/test/calculation",
        "origin": "adopted",
        "scheduler_job_id": "42",
        "identity": {"user": "fixture"},
        "status": "running",
        "terminal": False,
    }
    return placement, desired, scheduler, record, saved, commands, call


async def test_offline_remote_completion_survives_and_never_submits(placements, tmp_path):
    placement, desired, scheduler, record, saved, commands, call = placements
    assert (await placement.inspect("session", record))["status"] == "running"
    assert record["monitor"]["host"] == "cluster-a"
    # No platform process/model calls between these observations.
    scheduler.update(status="finished", terminal=True, exit_status=0)
    monitor_worker.tick(tmp_path / "cluster-a")
    restored = deepcopy(saved[-1])
    result = await placement.inspect("session", restored)
    assert result["terminal"] and result["scheduler_job_id"] == "42"
    assert (await placement.inspect("session", restored)) == result
    watch = json.loads((tmp_path / "cluster-a/watch-test-job.json").read_text())
    assert watch["request"]["action"] == "observe_external"
    assert len(commands) == 1  # Starts one lightweight observer, never sbatch.


async def test_handoff_fences_old_worker_and_local_monitor_is_independent(placements, tmp_path):
    placement, desired, scheduler, record, saved, commands, call = placements
    await placement.inspect("session", record)
    desired[0] = "cluster-b"
    await placement.inspect("session", record)
    old = json.loads((tmp_path / "cluster-a/watch-test-job.json").read_text())
    current = json.loads((tmp_path / "cluster-b/watch-test-job.json").read_text())
    assert not old["active"] and current["active"]
    assert (await call("cluster-a", record, 1, "enable"))["fenced"]
    desired[0] = "local"
    assert await placement.inspect("session", record) is None
    assert not json.loads((tmp_path / "cluster-b/watch-test-job.json").read_text())["active"]
    assert record["monitor"] == {"host": "local", "generation": 3}


async def test_unreachable_target_keeps_old_observer_and_interrupted_enable_reconciles(
    placements, tmp_path, monkeypatch
):
    placement, desired, scheduler, record, saved, commands, call = placements
    await placement.inspect("session", record)
    desired[0] = "cluster-b"
    fail = ["prepare"]

    async def fault(host, record, generation, operation):
        if host == "cluster-b" and operation == fail[0]:
            if operation == "enable":
                await call(host, record, generation, operation)  # Lost acknowledgement.
            raise AgentError("offline", "connection unavailable", 503)
        return await call(host, record, generation, operation)

    monkeypatch.setattr(placement, "call", fault)
    assert (await placement.inspect("session", record))["status"] == "running"
    assert json.loads((tmp_path / "cluster-a/watch-test-job.json").read_text())["active"]
    fail[0] = "enable"
    with pytest.raises(AgentError):
        await placement.inspect("session", record)
    assert record["monitor"]["handoff"]["generation"] == 2
    # Restart resumes generation 2; no new generation or job is created.
    restored = deepcopy(saved[-1])
    fail[0] = None
    await placement.inspect("session", restored)
    assert restored["monitor"]["host"] == "cluster-b"
    assert restored["monitor"]["generation"] == 2
    assert not json.loads((tmp_path / "cluster-a/watch-test-job.json").read_text())["active"]


def test_observer_rejects_submission_requests(tmp_path):
    with pytest.raises(ValueError, match="observation only"):
        monitor_worker.observe(tmp_path, {"request": {"action": "submit"}})


def test_detached_observer_continues_and_retains_completion(tmp_path, monkeypatch):
    """The control channel can exit while the independent worker keeps polling."""
    from chemistry_agent.hosting.monitor import WORKER

    source = WORKER.replace("poll_seconds = 30", "poll_seconds = 0.05")
    state = tmp_path / "queue-state.json"
    state.write_text(json.dumps({"status": "running", "terminal": False}))
    reader = (
        "import json,sys\nfrom pathlib import Path\n"
        "request=json.load(sys.stdin)\nprint(Path(request['root']).read_text())\n"
    )
    payload = {
        "directory": str(tmp_path / "spool"),
        "key": "job-42",
        "generation": 1,
        "operation": "enable",
        "request": {"root": str(state), "action": "status"},
        "reader": reader,
        "worker": source,
    }
    children = []
    original = subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(monitor_worker.subprocess, "Popen", capture)

    def until(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        pytest.fail("Detached monitor did not record the scheduler change")

    path = tmp_path / "spool/watch-job-42.json"
    try:
        monitor_worker.control(payload)
        monitor_worker.control(payload)  # Lost response retry: worker.lock admits one process.
        until(lambda: "observed_at" in json.loads(path.read_text()))
        until(lambda: sum(process.poll() is None for process in children) == 1)
        state.write_text(json.dumps({"status": "finished", "terminal": True, "exit_status": 0}))
        until(lambda: json.loads(path.read_text()).get("result", {}).get("terminal"))
        response = monitor_worker.control(payload | {"operation": "read"})
        assert response["result"]["exit_status"] == 0
        assert response["generation"] == 1
    finally:
        for process in children:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
