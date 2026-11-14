import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.automation.local_ssh import discover_aliases, enabled_aliases, save_aliases
from chemistry_agent.automation.slurm_jobs import SlurmJobs
from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.shell import ShellInput, remote_shell_tool
from chemistry_agent.tools import ToolContext


def test_alias_discovery_includes_and_selection(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text(
        "Host cluster-b\n HostName 172.27.11.111\n User researcher\n"
        "Include extra\nHost *\n IdentityFile private-key\n"
    )
    (ssh / "extra").write_text("Host cluster-a other\n HostName shared\nInclude config\n")
    rows = discover_aliases(ssh / "config")
    assert [row["alias"] for row in rows] == ["cluster-a", "cluster-b", "other"]
    assert rows[1]["hostname"] == "172.27.11.111"
    assert "private-key" not in json.dumps(rows)
    save_aliases(tmp_path, ["cluster-b"])
    assert enabled_aliases(tmp_path, ("cluster-a",)) == ("cluster-b",)
    save_aliases(tmp_path, [])
    assert enabled_aliases(tmp_path, ("cluster-a",)) == ()


def test_selection_api_without_aiida(settings_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chemistry_agent.automation.ssh_routes.discover_aliases",
        lambda: [{"alias": "cluster-b", "source": "~/.ssh/config"}],
    )
    app = create_app(settings_factory(automation_enabled=True, ssh_hosts="cluster-b"))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/automation/ssh/local-aliases").json()["servers"][0]["enabled"]
        assert (
            client.put("/api/automation/ssh/local-aliases", json={"hosts": []}).status_code == 200
        )
        assert not client.get("/api/automation/ssh/local-aliases").json()["servers"][0]["enabled"]
        assert (
            client.put("/api/automation/ssh/local-aliases", json={"hosts": ["unknown"]}).status_code
            == 422
        )
        assert (
            client.put("/api/automation/ssh/local-aliases", json={"hosts": ["cluster-b"]}).status_code
            == 200
        )
        assert client.get("/health").json()["ssh_hosts"] == ["cluster-b"]


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "", "ready": True, "queues": "low"},
        {"error": "", "cancel_requested": True},
        {"error": "sinfo failed", "ready": False},
    ],
)
async def test_ssh_response_distinguishes_empty_and_real_errors(tmp_path, monkeypatch, payload):
    class Process:
        returncode = 0

        async def communicate(self, data):
            return json.dumps(payload).encode(), b""

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    settings = SimpleNamespace(aiida_path=tmp_path, allowed_ssh_hosts=("cluster-b",))
    jobs = SlurmJobs(SimpleNamespace(service=SimpleNamespace(settings=settings)))
    if payload["error"]:
        with pytest.raises(AgentError, match="sinfo failed"):
            await jobs.request("cluster-b", {"action": "environment"})
    else:
        assert await jobs.request("cluster-b", {"action": "environment"}) == payload


async def test_enabled_shell_host_needs_no_turn_header_and_revokes_immediately(
    tmp_path, monkeypatch
):
    import asyncio

    class Process:
        returncode = 0
        stdin = SimpleNamespace(write=lambda data: None, close=lambda: None)
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()

        async def wait(self):
            return 0

    async def drain():
        pass

    Process.stdin.drain = drain
    Process.stdout.feed_data(b"cluster-b-node8\n")
    Process.stdout.feed_eof()
    Process.stderr.feed_eof()

    async def create(*args, **kwargs):
        assert "cluster-b" in args
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    save_aliases(tmp_path, ["cluster-b"])
    tool = remote_shell_tool((), tmp_path)
    args = ShellInput(host="cluster-b", command="hostname")
    assert (await tool.handler(args, ToolContext("session", "run")))["ok"]
    save_aliases(tmp_path, [])
    with pytest.raises(AgentError, match="SSH"):
        await tool.handler(args, ToolContext("session", "run"))
