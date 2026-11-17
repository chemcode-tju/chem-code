"""Retiring the old engine must not discard data or revive execution paths."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from chemistry_agent.api import create_app
from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.automation.task_files import relative_path
from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError


def test_platform_path_preserves_legacy_environment_and_new_precedence(tmp_path, monkeypatch):
    old = tmp_path / "old-records"
    new = tmp_path / "platform"
    monkeypatch.setenv("CHEMISTRY_AIIDA_PATH", str(old))
    monkeypatch.delenv("CHEMISTRY_PLATFORM_PATH", raising=False)
    assert Settings(_env_file=None).platform_path == old
    monkeypatch.setenv("CHEMISTRY_PLATFORM_PATH", str(new))
    assert Settings(_env_file=None).platform_path == new
    assert not old.exists() and not new.exists()


async def test_removed_worker_is_unreachable_and_structure_validation_still_works(
    settings_factory, monkeypatch
):
    async def forbidden(*args, **kwargs):
        pytest.fail("retired worker was started")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    service = AutomationService(settings_factory())
    for action in ["submit", "preview", "settings_initialize", "stop_plan_jobs"]:
        with pytest.raises(AgentError) as error:
            await service.call(action, {})
        assert error.value.status_code == 410
    result = await service.call(
        "validate_structure",
        {"content": "Cu\n1\n3 0 0\n0 3 0\n0 0 3\nCu\n1\nDirect\n0 0 0\n", "format": "vasp"},
    )
    assert "extxyz" in result


async def test_historical_plan_stays_readable_and_unchanged(settings_factory):
    chat = CalculationChat(AutomationService(settings_factory()))
    session, identifier = str(uuid4()), str(uuid4())
    plan = {
        "id": identifier,
        "status": "draft",
        "revision": 1,
        "nodes": [{"id": "old", "kind": "vasp", "title": "Historical result", "depends_on": []}],
        "runtime": {"old": {"status": "completed", "outputs": {"energy": -12.3}}},
    }
    chat.save(session, "plans", identifier, plan)
    path = chat.directory(session, "plans") / f"{identifier}.json"
    before = path.read_bytes()
    assert chat.plans.records(session)[0]["read_only"]
    for action in ["pause", "resume", "rediagnose"]:
        with pytest.raises(AgentError):
            await chat.plans.control(session, identifier, action)
    with pytest.raises(AgentError):
        await chat.plans.confirm(session, identifier, 1)
    await chat.plans.tick_plan(session, identifier)
    assert path.read_bytes() == before
    assert json.loads(before)["runtime"]["old"]["outputs"]["energy"] == -12.3


async def test_old_configuration_and_submission_endpoints_are_removed(settings_factory):
    app = create_app(settings_factory(automation_enabled=True))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        for url in ["settings/initialize", "submissions", "previews", "codes"]:
            assert (await client.post(f"/api/automation/{url}", json={})).status_code == 404
    schemas = CalculationChat(app.state.automation).tools()
    assert not {t.name for t in schemas} & {
        "calculation_initialize",
        "calculation_preview",
        "calculation_jobs",
    }


@pytest.mark.parametrize(
    "name", ["/etc/passwd", "../input", "nested/../../input", "archive/job", "x\x00y"]
)
def test_relative_path_keeps_file_safety_without_old_backend(name):
    with pytest.raises(AgentError):
        relative_path(name)
    assert relative_path("results/energy.dat") == "results/energy.dat"


def test_package_no_longer_registers_workflow_engine_plugins():
    import tomllib

    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    assert not any(k.startswith("aiida.") for k in project.get("entry-points", {}))
    assert "aiida" not in project["optional-dependencies"]


def test_plan_schema_advertises_only_current_execution_kinds():
    from chemistry_agent.automation.plan_models import PlanNode

    kinds = PlanNode.model_json_schema()["properties"]["kind"]["enum"]
    assert set(kinds) == {"task", "job", "model", "analysis"}
