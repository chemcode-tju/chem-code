"""Real local structures must remain usable without importing AiiDA or invoking a model."""

import hashlib
import io
import sys
from uuid import uuid4

import pytest
from ase import Atoms
from ase.constraints import FixAtoms
from ase.io import read, write
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.backends.service import BackendRouter
from chemistry_agent.errors import AgentError
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolRegistry
from chemistry_agent.workspace_files import WorkspaceFiles


def test_publish_originals_versions_reuse_and_session_boundary(settings_factory, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiida", None)
    settings = settings_factory(automation_enabled=True)
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
    ) as client:
        session = client.post("/sessions", json={"title": "structures"}).json()["id"]
        other = client.post("/sessions", json={"title": "other"}).json()["id"]
        workspace = settings.database_path.parent / "agent-workspaces" / session
        workspace.mkdir(parents=True)
        atoms = Atoms("Cu2", positions=[[0, 0, 0], [1.8, 1.8, 1.8]], cell=[6, 6, 6], pbc=True)
        atoms.set_constraint(FixAtoms(indices=[0]))
        source = workspace / "candidate.vasp"
        write(source, atoms, format="vasp")
        original = source.read_bytes()
        write(workspace / "candidates.extxyz", [atoms, atoms.copy()], format="extxyz")
        (workspace / "platform-history.json").write_text('{"private": true}')
        (workspace / "archive").mkdir()
        (workspace / "archive" / "old.xyz").write_text("do not open")
        (workspace / "leak.txt").symlink_to(settings.database_path)
        endpoint = f"/api/workspaces/{session}/publish"
        result = client.post(endpoint, json={}).json()
        assert not result["errors"], result
        assert {f["name"] for f in result["files"]} == {"candidate.vasp", "candidates.extxyz"}
        vasp = next(f for f in result["files"] if f["name"].endswith(".vasp"))
        assert vasp["sha256"] == hashlib.sha256(original).hexdigest()
        assert client.get(vasp["download_url"]).content == original
        parsed = read(io.StringIO(original.decode()), format="vasp")
        assert list(parsed.constraints[0].index) == [0]
        imported = client.post(
            f"/api/automation/chat/{session}/structures", json={"file_id": vasp["id"]}
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["positions_dof"] == [[False, False, False], [True, True, True]]
        assert (
            client.post(
                f"/api/automation/chat/{other}/structures", json={"file_id": vasp["id"]}
            ).status_code
            == 404
        )
        assert client.post(endpoint, json={}).json()["files"] == result["files"]
        source.write_bytes(original + b"\n")
        updated = client.post(endpoint, json={}).json()
        newer = next(f for f in updated["files"] if f["name"].endswith(".vasp"))
        assert newer["id"] != vasp["id"]
        assert client.get(vasp["download_url"]).content == original
        assert client.get(newer["download_url"]).content == original + b"\n"
        for forbidden in ["../other.txt", "platform-history.json", "archive/old.xyz", "leak.txt"]:
            assert client.post(endpoint, json={"paths": [forbidden]}).json()["errors"]
        assert client.get(f"/api/workspaces/{other}/files").json()["files"] == []


def test_default_platform_catalog_and_settings_do_not_require_aiida(settings_factory, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiida", None)
    with TestClient(create_app(settings_factory(automation_enabled=True))) as client:
        assert "aiida_enabled" not in client.get("/health").json()
        names = {tool["function"]["name"] for tool in client.get("/tools").json()}
        assert {
            "workspace_publish",
            "build_structure",
            "calculation_job_adopt",
            "calculation_wait_register",
            "calculation_plan_save",
        } <= names
        assert (
            not {
                "calculation_setup",
                "calculation_prepare",
                "calculation_job",
                "ssh_server_register",
                "calculation_settings",
            }
            & names
        )
        assert client.app.state.task_monitor is None


async def test_backend_completion_discovers_outputs_without_a_publish_tool_call(tmp_path):
    store = await Store.open(tmp_path / "store.sqlite3")
    try:
        session = (await store.create_session("native terminal"))["id"]
        run_id = str(uuid4())
        root = tmp_path / "agent-workspaces"
        workspace = root / session
        workspace.mkdir(parents=True)

        class NativeLikeAgent:
            async def run(self, messages, progress, **kwargs):
                (workspace / "molecule.xyz").write_text("1\nH atom\nH 0 0 0\n")
                return {"messages": [{"role": "assistant", "content": "created"}]}

        router = BackendRouter(store, NativeLikeAgent(), ToolRegistry(), root, "")
        await router.initialize()
        router.workspace_files = WorkspaceFiles(store, root)
        await router.workspace_files.initialize()
        await router.run([], None, session_id=session, run_id=run_id)
        files = await router.workspace_files.list(session)
        assert len(files) == 1
        assert files[0]["run_id"] == run_id
        assert files[0]["name"] == "molecule.xyz"
        await router.run([], None, session_id=session, run_id=str(uuid4()))
        assert len(await router.workspace_files.list(session)) == 1
    finally:
        await store.close()


async def test_disabled_legacy_backend_cannot_launch_a_worker(settings_factory, monkeypatch):
    async def forbidden_worker(*args, **kwargs):
        raise AssertionError("No AiiDA worker may start in generic platform mode")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden_worker)
    service = AutomationService(settings_factory(automation_enabled=True))
    with pytest.raises(AgentError, match="旧执行后端已移除"):
        await service.call("submit", {"preview_id": str(uuid4())})
