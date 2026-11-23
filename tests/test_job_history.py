"""Retained Slurm history survives replay, purge, and individual uncovered dates."""

from datetime import datetime, timedelta

from chemistry_agent.automation.job_history import JobHistory
from chemistry_agent.errors import AgentError


async def test_retained_jobs_merge_after_purge_without_filling_gaps(settings_factory):
    settings = settings_factory(automation_enabled=True, slurm_history_hosts="cluster-b")
    today = datetime.now().astimezone().replace(hour=8, minute=0, second=0, microsecond=0)
    dates = [(today.date() - timedelta(days=offset)).isoformat() for offset in range(7)]
    old_job = {
        "id": "10_1",
        "submitted": f"{dates[6]}T01:00:00",
        "status": "COMPLETED",
        "ended": f"{dates[6]}T03:00:00",
        "cpu_seconds": "7200",
        "elapsed_seconds": "3600",
        "resources": "cpu=2,gres/gpu=1",
        "gpu_tracked": True,
        "inferred_gpu": "v100",
    }
    pending = old_job | {
        "id": "10_2",
        "submitted": f"{dates[4]}T07:00:00",
        "status": "PENDING",
        "ended": "",
        "cpu_seconds": "",
        "elapsed_seconds": "",
        "resources": "",
        "inferred_gpu": None,
    }
    first = {
        "observed_at": (today - timedelta(days=3)).isoformat(),
        "account": "owner",
        "accounting_available": True,
        "active": {"queued": 1, "running": 0, "other": 0},
        "coverage_start": dates[6],
        "gpu_tracked": True,
        "records": [old_job, pending],
    }
    finished = pending | {
        "status": "FAILED",
        "ended": f"{dates[1]}T09:00:00",
        "cpu_seconds": "3600",
        "elapsed_seconds": "1800",
        "resources": "cpu=2,gres/gpu=2",
        "inferred_gpu": "a6000",
    }
    newer = first | {
        "observed_at": today.isoformat(),
        "coverage_start": dates[1],
        "records": [finished],
    }

    class Scheduler:
        def __init__(self) -> None:
            self.settings = settings
            self.offline = False

        def hosts(self) -> list[str]:
            return ["cluster-b"]

        async def request(self, host: str, payload: dict) -> dict:
            assert host == "cluster-b" and payload["action"] == "queue_dashboard_snapshot"
            if self.offline:
                raise AgentError("job_ssh_failed", "offline", 503)
            return newer

    scheduler = Scheduler()
    history = JobHistory(scheduler)
    history.save("cluster-b", first)
    history.save("cluster-b", first)
    history.save("cluster-b", newer)
    history.save("cluster-b", first)

    reopened = JobHistory(scheduler)
    result = await reopened.dashboard("cluster-b", "all")
    by_day = {row["date"]: row for row in result["days"]}
    assert result["period_start"] == dates[6]
    assert sum(row["submitted"] or 0 for row in by_day.values()) == 2
    assert by_day[dates[6]]["gpu_seconds"] == 3600
    assert by_day[dates[1]]["failed"] == 1
    assert by_day[dates[1]]["gpu_seconds"] == 3600
    assert by_day[dates[5]]["submitted"] == 0
    assert by_day[dates[3]]["submitted"] is None
    assert by_day[dates[2]]["submitted"] is None
    assert result["history_synced_at"] == newer["observed_at"]
    assert reopened.read("cluster-b", newer | {"account": "different"}, "all") is None

    scheduler.offline = True
    retained = await reopened.dashboard("cluster-b", "all")
    assert not retained["live_accounting_available"]
    assert retained["accounting_available"] and retained["active"] is None
    assert retained["days"][-1]["submitted"] is None
    assert sum(row.get("gpu_seconds", 0) for row in retained["days"]) == 7200
