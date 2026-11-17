"""Real persistence and HTTP/MCP contracts; no cluster, model, or production data."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chemistry_agent.errors import AgentError
from chemistry_agent.research_plugins.daemon import create_app
from chemistry_agent.research_plugins.ledger import MonitorLedger
from chemistry_agent.research_plugins.models import JobRegister, WaitControl, WaitRegister
from chemistry_agent.research_plugins.observer import scheduler_row
from chemistry_agent.research_plugins.service import ResearchService


def register(ledger: MonitorLedger, workspace: str, key: str, **kwargs) -> dict:
    return ledger.register(JobRegister(workspace_id=workspace, key=key, name=key, **kwargs))


def test_restart_late_wait_and_exact_ack(tmp_path):
    workspace, thread = str(uuid4()), str(uuid4())
    ledger = MonitorLedger(tmp_path / "state.sqlite3")
    job = register(ledger, workspace, "first", duration_seconds=0)
    assert register(ledger, workspace, "first", duration_seconds=0)["id"] == job["id"]
    with pytest.raises(ValueError):
        register(ledger, workspace, "first", duration_seconds=1)
    ledger.tick_simulations()
    # Job completes before registration; restarting the daemon must not lose the event.
    ledger = MonitorLedger(ledger.path)
    args = WaitRegister(workspace_id=workspace, thread_id=thread, job_ids=[job["id"]])
    wait = ledger.register_wait(args)
    assert wait["status"] == "ready"
    assert wait["event"]["data"]["jobs"][0]["scientific_convergence"] == "not_assessed"
    assert wait["automatic_wakeup"] == "unavailable"
    assert ledger.register_wait(args) == wait
    with pytest.raises(ValueError):
        ledger.control_wait(
            WaitControl(
                workspace_id=workspace, wait_id=wait["id"], action="acknowledge", event_id=uuid4()
            )
        )
    control = WaitControl(
        workspace_id=workspace,
        wait_id=wait["id"],
        action="acknowledge",
        event_id=wait["event"]["id"],
    )
    assert ledger.control_wait(control)["status"] == "acknowledged"
    assert MonitorLedger(ledger.path).control_wait(control)["event"]["acknowledged"]


def test_all_any_multiple_chats_cancel_and_unknown(tmp_path):
    workspace = str(uuid4())
    ledger = MonitorLedger(tmp_path / "state.sqlite3")
    jobs = [
        register(ledger, workspace, key, backend="slurm", host="cluster-b", scheduler_id=key)
        for key in ("100", "101")
    ]
    waits = [
        ledger.register_wait(
            WaitRegister(
                workspace_id=workspace,
                thread_id=uuid4(),
                job_ids=[job["id"] for job in jobs],
                mode=mode,
            )
        )
        for mode in ("all", "any", "all")
    ]
    ledger.control_wait(
        WaitControl(workspace_id=workspace, wait_id=waits[2]["id"], action="cancel")
    )
    ledger.observe(jobs[0]["id"], "UNKNOWN", error="SSH disconnected")
    assert ledger.read_wait(workspace, waits[1]["id"])["status"] == "waiting"
    ledger.observe(jobs[0]["id"], "FAILED", "1:0")
    assert ledger.read_wait(workspace, waits[0]["id"])["status"] == "waiting"
    assert ledger.read_wait(workspace, waits[1]["id"])["status"] == "ready"
    ledger.observe(jobs[1]["id"], "COMPLETED", "0:0")
    assert ledger.read_wait(workspace, waits[0]["id"])["status"] == "ready"
    assert ledger.read_wait(workspace, waits[2]["id"])["event"] is None
    with pytest.raises(ValueError):
        ledger.read_wait(str(uuid4()), waits[0]["id"])
    with pytest.raises(ValueError):
        ledger.register_wait(
            WaitRegister(workspace_id=uuid4(), thread_id=uuid4(), job_ids=[jobs[0]["id"]])
        )


def test_slurm_accounting_never_uses_batch_row():
    assert scheduler_row("100.batch|COMPLETED|0:0\n", "100") == ("UNKNOWN", None)
    assert scheduler_row("100|CANCELLED by 22|0:15\n", "100") == ("CANCELLED", "0:15")
    assert scheduler_row("100_3|RUNNING\n", "100_3") == ("RUNNING", None)


async def test_shared_plan_revision_binding_and_folders(tmp_path):
    service = ResearchService(tmp_path, project_roots=(tmp_path,))
    workspace = str(uuid4())
    await service.execute(
        "workspace_open", {"workspace_id": workspace, "project_path": str(tmp_path)}
    )
    draft = {
        "title": "Cu CO 研究",
        "goal": "比较结构",
        "nodes": [
            {"id": "prepare", "title": "准备 Cu 表面", "kind": "task"},
            {"id": "relax", "title": "优化 CO 吸附结构", "kind": "task", "depends_on": ["prepare"]},
        ],
    }
    plan = await service.execute("research_save_plan", {"workspace_id": workspace, "draft": draft})
    revised = draft | {"plan_id": plan["id"], "expected_revision": 1, "title": "修订方案"}
    second = await service.execute(
        "research_save_plan", {"workspace_id": workspace, "draft": revised}
    )
    assert second["revision"] == 2
    with pytest.raises(AgentError):
        await service.execute("research_save_plan", {"workspace_id": workspace, "draft": revised})
    assert service.chat.list_records(workspace, "plan_versions")[0]["title"] == draft["title"]
    job = await service.execute(
        "monitor_register_job",
        {
            "workspace_id": workspace,
            "key": "relax",
            "name": "模拟结构优化",
            "plan_id": plan["id"],
            "node_id": "relax",
        },
    )
    folder = tmp_path / "calculation"
    folder.mkdir()
    (folder / "README.md").write_text("fixture")
    (folder / "nested").mkdir()
    (folder / ".hidden").write_text("not listed")
    (folder / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    # Name-only exclusion; no archive content is created or inspected by this test.
    link = await service.execute(
        "research_link_folder",
        {
            "workspace_id": workspace,
            "plan_id": plan["id"],
            "node_id": "relax",
            "path": "calculation",
        },
    )
    restarted = ResearchService(tmp_path, project_roots=(tmp_path,))
    data = await restarted.execute("research_read_plan", {"workspace_id": workspace})
    assert data["jobs"][0]["id"] == job["id"]
    assert data["folders"][0]["id"] == link["id"]
    args = {"workspace_id": workspace, "plan_id": plan["id"], "node_id": "relax"}
    listing = await restarted.execute("research_read_folder", args)
    assert [entry["name"] for entry in listing["entries"]] == ["nested", "README.md"]
    child = await restarted.execute("research_read_folder", args | {"path": "nested"})
    assert child["parent"] == "."
    for bad_path in ("../..", "archive", ".hidden", "escape"):
        with pytest.raises(ValueError):
            await restarted.execute("research_read_folder", args | {"path": bad_path})
    assert data["plans"][0]["runtime"]["relax"]["status"] == "pending"


def test_authenticated_daemon_and_background_simulation(tmp_path):
    app = create_app(tmp_path)
    headers = {"Authorization": f"Bearer {(tmp_path / 'access-token').read_text()}"}
    workspace = str(uuid4())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        assert (
            client.get("/health", headers=headers | {"Origin": "https://example.org"}).status_code
            == 401
        )
        assert client.get("/health", headers=headers).json()["model_runtime"] is False

        def call(name: str, args: dict) -> dict:
            response = client.post(f"/tools/{name}", headers=headers, json=args)
            assert response.status_code == 200, response.text
            return response.json()

        call("workspace_open", {"workspace_id": workspace})
        job = call(
            "monitor_register_job",
            {"workspace_id": workspace, "key": "auto", "name": "test", "duration_seconds": 0},
        )
        wait = call(
            "monitor_register_wait",
            {"workspace_id": workspace, "thread_id": str(uuid4()), "job_ids": [job["id"]]},
        )
        ready = call(
            "monitor_read_wait",
            {"workspace_id": workspace, "wait_id": wait["id"], "timeout_seconds": 3},
        )
        assert ready["status"] == "ready"
