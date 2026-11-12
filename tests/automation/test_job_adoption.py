"""Existing allocations must be observed, never implicitly submitted again."""

import asyncio
import subprocess
from uuid import uuid4

import pytest
from test_slurm_jobs import cluster as cluster_fixture
from test_slurm_jobs import tick

from chemistry_agent.automation import slurm_remote
from chemistry_agent.automation.job_adoption import adopt_job
from chemistry_agent.automation.job_models import JobAdopt
from chemistry_agent.automation.plan_authority import auto_queue, repair_target
from chemistry_agent.automation.plan_models import NodeRepair
from chemistry_agent.automation.wait_ledger import WaitLedger
from chemistry_agent.errors import AgentError
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolContext

cluster = cluster_fixture


def scheduler(monkeypatch, directory, *, state="RUNNING", exit_code="0:0", extra=""):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv == ["scontrol", "show", "config"]:
            output = "ClusterName = laboratory\n"
        elif argv[:4] == ["scontrol", "-o", "show", "job"]:
            output = (
                f"JobId=42 UserId=alice(1000) JobName=external {extra} "
                f"SubmitTime=2026-11-12T10:00:00 WorkDir={directory} "
                f"JobState={state} ExitCode={exit_code} Partition=debug Account=lab"
            )
        else:
            raise AssertionError(f"Registration must not mutate scheduler: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(slurm_remote, "run", run)
    return calls


def test_observation_has_pinned_identity_without_our_exit_marker(tmp_path, monkeypatch):
    scheduler(monkeypatch, tmp_path, state="COMPLETED")
    result = slurm_remote.external_job({"scheduler_job_id": "42"})
    assert result["finished_ok"]
    assert result["identity"]["submitted_at"] == "2026-11-12T10:00:00"
    assert not list(tmp_path.iterdir())
    identity = result["identity"] | {"submitted_at": "2026-11-11T10:00:00"}
    mismatch = slurm_remote.external_job({"scheduler_job_id": "42", "identity": identity})
    assert not mismatch["known"] and not mismatch["terminal"]


def test_accounting_unknown_and_duplicate_ids_are_not_completion(tmp_path, monkeypatch):
    rows = []

    def run(argv):
        if argv == ["scontrol", "show", "config"]:
            return subprocess.CompletedProcess(argv, 0, "ClusterName = laboratory\n", "")
        return subprocess.CompletedProcess(
            argv, 0 if argv[0] == "sacct" else 1, "\n".join(rows), ""
        )

    monkeypatch.setattr(slurm_remote, "run", run)
    request = {"scheduler_job_id": "42"}
    assert not slurm_remote.external_job(request)["terminal"]
    rows.extend(
        f"42|laboratory|alice|2026-11-{day}T10:00:00|{tmp_path}|COMPLETED|0:0|debug|lab||"
        for day in (11, 12)
    )
    assert not slurm_remote.external_job(request)["known"]
    result = slurm_remote.external_job(request | {"submitted_at": "2026-11-12T10:00:00"})
    assert result["finished_ok"] and result["source"] == "sacct"


def test_array_requires_element_and_cancel_checks_identity(tmp_path, monkeypatch):
    calls = scheduler(monkeypatch, tmp_path, extra="ArrayJobId=42 ArrayTaskId=3")
    with pytest.raises(ValueError, match="数组父任务"):
        slurm_remote.external_job({"scheduler_job_id": "42"})
    assert slurm_remote.external_job({"scheduler_job_id": "42_3"})["known"]
    result = slurm_remote.dispatch(
        {"root": str(tmp_path), "action": "cancel_external", "scheduler_job_id": "42_3"}
    )
    assert not result["cancel_requested"]
    assert all(argv[0] != "scancel" for argv in calls)


async def test_concurrent_registration_restart_and_completion_exactly_once(cluster, monkeypatch):
    plans, context, tmp = cluster
    remote = tmp / "external"
    remote.mkdir()
    (remote / "result.out").write_text("real-output")
    calls = scheduler(monkeypatch, remote)
    store = await Store.open(tmp / "adopt.sqlite3")
    context = ToolContext((await store.create_session("external"))["id"], context.run_id)
    plans.store = store
    args = JobAdopt(host="test", scheduler_job_id="42", retrieve=["result.out"])
    try:
        first, repeated = await asyncio.gather(
            adopt_job(plans, args, context), adopt_job(plans, args, context)
        )
        assert first["job_id"] == repeated["job_id"]
        assert sum(item["reused"] for item in (first, repeated)) == 1
        assert not first["submitted"]
        current = await tick(plans, context, first["plan_id"])
        assert current["status"] == "running"
        assert len(current["nodes"]) == 1
        # Full store and monitor restart while the allocation finishes.
        await store.close()
        store = await Store.open(tmp / "adopt.sqlite3")
        plans.store = store
        scheduler(monkeypatch, remote, state="COMPLETED")
        current = await tick(plans, context, first["plan_id"])
        assert current["status"] == "completed"
        ledger = WaitLedger(store)
        await ledger.observe(context.session_id, current)
        event = await ledger.pending(context.session_id, current)
        assert event and event["session_id"] == context.session_id
        assert (
            "real-output"
            == next(
                plans.chat.directory(context.session_id, "plan_runs").rglob("result.out")
            ).read_text()
        )
        again = await adopt_job(plans, args, context)
        assert again["reused"] and again["job_id"] == first["job_id"]
        assert (await ledger.pending(context.session_id, current))["id"] == event["id"]
        assert all(argv[0] != "sbatch" for argv in calls)
        assert sorted(p.name for p in remote.iterdir()) == ["result.out"]
    finally:
        await store.close()


async def test_orphan_receipt_recovers_and_invalid_directory_is_rejected(cluster, monkeypatch):
    plans, context, tmp = cluster
    scheduler(monkeypatch, tmp)
    args = JobAdopt(host="test", scheduler_job_id="42")
    with pytest.raises(AgentError, match="WorkDir"):
        await adopt_job(plans, args.model_copy(update={"remote_directory": "/wrong"}), context)
    original = plans.save

    def crash(*args):
        raise RuntimeError("process interrupted")

    monkeypatch.setattr(plans, "save", crash)
    with pytest.raises(RuntimeError):
        await adopt_job(plans, args, context)
    monkeypatch.setattr(plans, "save", original)
    result = await adopt_job(plans, args, context)
    assert result["reused"]
    assert len(plans.chat.load(context.session_id, "plans", result["plan_id"])["nodes"]) == 1
    other = await adopt_job(plans, args, ToolContext(str(uuid4()), str(uuid4())))
    assert other["job_id"] != result["job_id"]  # Each conversation gets its own wake-up.


async def test_long_wake_instruction_survives_retry_without_invalid_node(cluster, monkeypatch):
    plans, context, tmp = cluster
    scheduler(monkeypatch, tmp)
    instruction = "检查输出并继续已授权目标。" * 500
    args = JobAdopt(host="test", scheduler_job_id="42", instruction=instruction)
    result = await adopt_job(plans, args, context)
    record = plans.slurm.load(context.session_id, result["job_id"])
    assert record["instruction"] == instruction
    plan = plans.chat.load(context.session_id, "plans", result["plan_id"])
    assert len(plan["nodes"][0]["purpose"]) == 1000
    assert (await adopt_job(plans, args, context))["reused"]


async def test_adopted_metadata_cannot_be_replayed_as_a_new_submission(cluster, monkeypatch):
    plans, context, tmp = cluster
    scheduler(monkeypatch, tmp, state="FAILED", exit_code="4:0")
    queue_token = auto_queue.set("debug")
    try:
        result = await adopt_job(plans, JobAdopt(host="test", scheduler_job_id="42"), context)
    finally:
        auto_queue.reset(queue_token)
    plan = await tick(plans, context, result["plan_id"])
    assert plan["status"] == "failed"
    token = repair_target.set((context.session_id, result["plan_id"]))
    try:
        with pytest.raises(AgentError, match="脚本和资源"):
            await plans.repair_node(
                NodeRepair(plan_id=result["plan_id"], node_id=result["node_id"], reason="retry"),
                context,
            )
    finally:
        repair_target.reset(token)


async def test_one_external_completion_releases_only_one_waiter_across_chats(cluster, monkeypatch):
    plans, context, tmp = cluster
    scheduler(monkeypatch, tmp, state="COMPLETED")
    args = JobAdopt(host="test", scheduler_job_id="42")
    first = await adopt_job(plans, args, context)
    second_context = ToolContext(str(uuid4()), str(uuid4()))
    second = await adopt_job(plans, args, second_context)
    assert first["job_id"] != second["job_id"]
    waiting = {
        "id": str(uuid4()),
        "host": "test",
        "user": "alice",
        "settings": {"resources": {"queue": "debug"}},
        "quota_wait_since": "2000",
    }
    assert plans.slurm.release_slot(waiting)
    assert plans.slurm.release_slot(waiting | {"id": str(uuid4())}) is None
