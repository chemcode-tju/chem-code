"""Settings migrations preserve node identity, Agent drafts and knowledge scope."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.automation.local_ssh import save_aliases
from chemistry_agent.backends.service import BackendRouter, BackendSettings, BackendUpdate
from chemistry_agent.config import Settings
from chemistry_agent.environments.registry import Environment, Environments
from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.shell import ShellInput, remote_shell_tool
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolContext, ToolRegistry


@pytest.fixture
def ssh_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chemistry_agent.environments.registry.discover_aliases",
        lambda: [{"alias": "cluster-a", "hostname": "cpu.example"}],
    )


def test_transport_switch_keeps_identity_and_removed_ssh_does_not_reappear(
    tmp_path: Path,
    ssh_aliases: None,
) -> None:
    save_aliases(tmp_path, ["cluster-a"])
    environments = Environments(tmp_path)
    assert [row["id"] for row in environments.inventory()] == ["cluster-a"]
    with pytest.raises(AgentError, match="节点已存在"):
        environments.save(Environment(id="cluster-a", name="CPU"), create=True)
    environments.save(Environment(id="cpu", name="CPU", transport="ssh", ssh_alias="cluster-a"))
    assert environments.available_hosts() == ["cpu"]
    current = environments.get("cpu")
    environments.save(current.model_copy(update={"transport": "runner"}))
    assert environments.available_hosts() == ["cpu"]
    assert [row["id"] for row in environments.inventory()] == ["cpu"]
    with pytest.raises(AgentError, match="另一个节点"):
        environments.save(Environment(id="duplicate", name="CPU", transport="ssh", ssh_alias="cluster-a"))
    environments.remove("cpu")
    assert environments.inventory() == []
    assert environments.available_hosts() == []
    # Legacy authorization remains on disk for compatibility; the tombstone wins.
    assert json.loads((tmp_path / "ssh/enabled_aliases.json").read_text())["hosts"] == ["cluster-a"]


async def test_ssh_node_routes_alias_but_returns_stable_identity(
    tmp_path: Path,
    ssh_aliases: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environments = Environments(tmp_path)
    environments.save(Environment(id="cpu", name="CPU", transport="ssh", ssh_alias="cluster-a"))
    monkeypatch.setattr(
        "chemistry_agent.runtime.shell.shell_profiles", lambda root: {"cluster-a": "fixture"}
    )

    async def execute(root: Path, identifier: str, host: str, command: str) -> dict:
        assert (identifier, host, command) == ("fixture", "cluster-a", "hostname")
        return {"host": host, "ok": True, "stdout": "cpu.example"}

    monkeypatch.setattr("chemistry_agent.runtime.shell.profiled_command", execute)
    tool = remote_shell_tool((), tmp_path)
    args, context = ShellInput(host="cpu", command="hostname"), ToolContext("test", "test")
    assert (await tool.handler(args, context))["host"] == "cpu"
    environments.save(environments.get("cpu").model_copy(update={"enabled": False}))
    with pytest.raises(AgentError, match="未启用"):
        await tool.handler(args, context)


def test_node_routes_toggle_remove_and_duplicate_without_connecting(
    settings_factory: Callable[..., Settings],
    ssh_aliases: None,
) -> None:
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        node = {"id": "cpu", "name": "CPU", "transport": "ssh", "ssh_alias": "cluster-a"}
        assert client.post("/api/environments", json=node).status_code == 200
        assert client.post("/api/environments", json=node).status_code == 409
        assert (
            client.post(
                "/api/environments", json={"id": "gpu", "name": "GPU", "transport": "local"}
            ).status_code
            == 200
        )
        assert client.patch("/api/environments/cpu", json={"enabled": False}).status_code == 200
        assert [row["id"] for row in client.get("/api/environments").json()["environments"]] == [
            "cpu",
            "gpu",
        ]
        assert client.post("/api/environments/cpu/probe").status_code == 409
        assert (
            client.put("/api/environments/cpu", json=node | {"transport": "local"}).status_code
            == 200
        )
        assert client.post("/api/environments/cpu/probe").json()["ok"] is True
        assert client.delete("/api/environments/cpu").status_code == 200
        assert client.delete("/api/environments/gpu").status_code == 200
        assert client.get("/api/environments").json()["environments"] == []
        assert client.post("/api/environments/cpu/probe").status_code == 409


async def test_concurrent_agent_cards_preserve_other_settings_and_existing_chat(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        router = BackendRouter(store, None, ToolRegistry(), tmp_path, "")
        await router.initialize()
        await router.save_settings(BackendSettings(permission_mode="full", codex_model="original"))
        session = (await store.create_session("existing"))["id"]
        original = await router.binding(session, create=True)
        await asyncio.gather(
            router.update_settings("codex", BackendUpdate(model="new-codex", make_default=True)),
            router.update_settings(
                "antigravity", BackendUpdate(model="new-agy", executable="/bin/agy")
            ),
        )
        config = await router.settings()
        assert (config.codex_model, config.antigravity_model) == ("new-codex", "new-agy")
        assert (config.default_backend, config.permission_mode) == ("codex", "full")
        assert await router.binding(session) == original
        with pytest.raises(AgentError):
            await router.update_settings("missing", BackendUpdate(make_default=True))
        assert (await router.settings()).default_backend == "codex"
    finally:
        await store.close()


def test_card_api_failure_is_atomic_and_mcp_project_config_is_usable(
    settings_factory: Callable[..., Settings],
) -> None:
    with TestClient(
        create_app(settings_factory()),
        base_url="http://127.0.0.1:8857",
        client=("127.0.0.1", 50000),
    ) as client:
        good = client.patch(
            "/api/agents/settings/codex", json={"model": "fixture", "make_default": True}
        )
        assert good.status_code == 200
        bad = client.patch(
            "/api/agents/settings/antigravity", json={"executable": "", "make_default": True}
        )
        assert bad.status_code == 422
        assert client.get("/api/agents/settings").json()["settings"] == good.json()
        metadata = client.get("/api/knowledge/connection").json()
        assert metadata["platform_origin"] == "http://127.0.0.1:8857"
        assert Path(metadata["stdio_command"]).is_file()
        assert (
            Path(metadata["stdio_env"]["PYTHONPATH"]) / "chemistry_agent/knowledge/mcp.py"
        ).is_file()
        assert (
            client.get(
                "/api/knowledge/connection", headers={"Origin": "https://unrelated.test"}
            ).status_code
            == 403
        )
        projects = [
            client.post("/api/projects", json={"name": name}).json()["id"] for name in ("Cu", "Pt")
        ]
        save = {
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_save",
                "arguments": {"title": "Cu setup", "content": "Use the optimized Cu cell"},
            },
        }
        result = client.post(
            metadata["http_path"], params={"project_id": projects[0]}, json=save
        ).json()["result"]
        assert not result.get("isError"), result
        memory = json.loads(result["content"][0]["text"])
        read = {
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_read", "arguments": {"memory_id": memory["id"]}},
        }
        assert (
            not client.post(metadata["http_path"], params={"project_id": projects[0]}, json=read)
            .json()["result"]
            .get("isError")
        )
        search = {
            "id": 3,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": {"scope": "project"}},
        }
        for project_id, expected in zip(projects, [1, 0], strict=True):
            found = client.post(
                metadata["http_path"], params={"project_id": project_id}, json=search
            ).json()["result"]
            assert len(json.loads(found["content"][0]["text"])["items"]) == expected
