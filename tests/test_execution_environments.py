"""Runner transport and durable receipts with real local files; never a cluster job."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.environments.registry import Environment, Environments
from chemistry_agent.environments.runner import RunnerOperations, create_runner
from chemistry_agent.errors import AgentError
from chemistry_agent.provider import FunctionCall, ToolCall
from chemistry_agent.runtime.shell import remote_shell_tool
from chemistry_agent.tools import ToolContext, ToolRegistry


async def test_local_receipt_replay_and_unknown_restart(tmp_path):
    runner = RunnerOperations(tmp_path / "runner")
    request = {"id": str(uuid4()), "command": "printf 'one\n' >> count", "cwd": str(tmp_path)}
    done = await runner.shell(request)
    assert done["state"] == "completed"
    assert (await RunnerOperations(runner.root).shell(request))["replayed"] is False
    assert (tmp_path / "count").read_text() == "one\n"
    with pytest.raises(ValueError):
        await runner.shell(request | {"command": "touch other"})
    path = runner.root / "commands" / f"{request['id']}.json"
    receipt = json.loads(path.read_text()) | {"state": "running"}
    path.write_text(json.dumps(receipt))
    assert (await RunnerOperations(runner.root).shell(request))["state"] == "unknown"
    assert (tmp_path / "count").read_text() == "one\n"


async def test_commands_can_run_concurrently_with_same_receipt_dedup(tmp_path):
    runner = RunnerOperations(tmp_path / "runner")
    request = {"id": str(uuid4()), "command": "sleep .1; echo once >> count", "cwd": str(tmp_path)}
    result = await asyncio.gather(runner.shell(request), runner.shell(request))
    assert any(r["state"] == "completed" for r in result)
    assert (tmp_path / "count").read_text() == "once\n"


async def test_http_runner_auth_and_host_registry(tmp_path):
    token = "test-token-long-enough-for-fixture"
    app = create_runner(tmp_path / "runner", token)
    with TestClient(app) as client:
        assert client.post("/v1/probe", json={}).status_code == 401
        assert (
            client.post(
                "/v1/probe",
                json={},
                headers={"Authorization": f"Bearer {token}", "Origin": "https://unrelated.example"},
            ).status_code
            == 401
        )
    environments = Environments(tmp_path, httpx.ASGITransport(app=app))
    environments.save(Environment(id="cluster-b", name="GPU", endpoint="http://runner", token=token))
    environments.save(Environment(id="cluster-a", name="CPU", transport="local", capabilities=["CPU"]))
    assert set(environments.hosts()) == {"cluster-a", "cluster-b"}
    assert "test-token" not in json.dumps(environments.public())
    assert (await environments.request("cluster-b", "probe"))["protocol"] == 1
    assert (await environments.request("cluster-a", "probe"))["cpu_count"] > 0
    environments.save(Environment(id="cluster-b", name="GPU", enabled=False))
    assert environments.get("cluster-b").token is None
    with pytest.raises(AgentError):
        await environments.request("cluster-b", "probe")


def test_empty_runner_credentials_rejected():
    with pytest.raises(ValueError):
        create_runner(token="short")


async def test_tool_recovery_reuses_receipt_but_new_call_executes(tmp_path):
    environments = Environments(tmp_path)
    environments.save(Environment(id="cpu", name="CPU", transport="local"))
    registry = ToolRegistry([remote_shell_tool((), tmp_path)])
    command = f"echo once >> '{tmp_path / 'count'}'"
    call = ToolCall(
        id="native-tool-1",
        type="function",
        function=FunctionCall(
            name="remote_shell", arguments=json.dumps({"host": "cpu", "command": command})
        ),
    )
    context = ToolContext("session-1", "run-1")
    first = await registry.execute(call, context)
    again = await registry.execute(call, context)
    assert first.success and again.success
    assert (tmp_path / "count").read_text() == "once\n"
    await registry.execute(call.model_copy(update={"id": "native-tool-2"}), context)
    assert (tmp_path / "count").read_text() == "once\nonce\n"
