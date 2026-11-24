"""Native ACP turns, MCP host context, permissions and durable no-replay."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.backends.antigravity import AntigravityBackend, model_catalog
from chemistry_agent.backends.mcp_bridge import PlatformMCPBridge
from chemistry_agent.errors import AgentError

OPTIONS = [
    {
        "id": "model",
        "category": "model",
        "currentValue": "gemini-test",
        "options": [{"value": "gemini-test", "name": "Gemini Test"}],
    },
    {"id": "thought", "category": "thought_level", "options": [{"value": "low", "name": "Low"}]},
    {"id": "mode", "category": "mode", "options": [{"value": "default"}, {"value": "auto"}]},
]


class FakeACP:
    requests = []
    prompts = 0
    failure = False
    permission = False
    decisions = []

    def __init__(self, executable, arguments, cwd):
        self.events = asyncio.Queue()
        self.initialized = {"agentCapabilities": {"loadSession": True}}
        self.answer = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, params, timeout=None):  # noqa: ASYNC109
        self.requests.append((method, params))
        if method in {"session/new", "session/load"}:
            self.bridge = params["mcpServers"]
            if method == "session/load":
                await self.update(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "OLD"},
                    }
                )
            return {"sessionId": "native-session", "configOptions": OPTIONS}
        if method == "session/set_config_option":
            return {"configOptions": OPTIONS}
        if method == "session/prompt":
            type(self).prompts += 1
            if self.failure:
                raise AgentError("acp_disconnected", "disconnect", 502)
            if self.permission:
                await self.events.put(
                    {
                        "method": "session/request_permission",
                        "id": 77,
                        "params": {
                            "sessionId": "native-session",
                            "toolCall": {"toolCallId": "t1", "title": "write a file"},
                            "options": [
                                {"kind": "allow_once", "optionId": "yes"},
                                {"kind": "reject_once", "optionId": "no"},
                            ],
                        },
                    }
                )
                await self.answer.wait()
            descriptor = self.bridge[0]
            env = {e["name"]: e["value"] for e in descriptor["env"]}
            reader, writer = await asyncio.open_unix_connection(env["CHEM_MCP_SOCKET"])
            writer.write(
                (
                    json.dumps(
                        {
                            "token": env["CHEM_MCP_TOKEN"],
                            "message": {
                                "id": 1,
                                "method": "tools/call",
                                "params": {
                                    "name": "molecular_properties",
                                    "arguments": {"smiles": "CO"},
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert not result["result"]["isError"]
            assert "CH4O" in result["result"]["content"][0]["text"]
            await self.update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "甲醇 CH4O"},
                }
            )
            await self.update({"sessionUpdate": "usage_update", "used": 60, "size": 10000})
            return {"stopReason": "end_turn"}
        return {}

    async def update(self, update):
        await self.events.put(
            {
                "method": "session/update",
                "params": {"sessionId": "native-session", "update": update},
            }
        )

    async def send(self, message):
        if "result" in message:
            self.decisions.append(message["result"])
            self.answer.set()


@pytest.fixture
def fake_acp(monkeypatch):
    FakeACP.requests, FakeACP.prompts, FakeACP.decisions = [], 0, []
    FakeACP.failure = FakeACP.permission = False
    original = AntigravityBackend.__init__

    def initialize(self, *args):
        original(self, *args)
        self.transport_factory = FakeACP

    monkeypatch.setattr(AntigravityBackend, "__init__", initialize)
    monkeypatch.setattr(AntigravityBackend, "executable", staticmethod(lambda config: "/fake/agy"))
    return FakeACP


def test_acp_platform_tools_continuity_and_settings(settings_factory, fake_acp):
    settings = settings_factory(deepseek_api_key="")
    for iteration in range(2):
        with TestClient(
            create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
        ) as client:
            if iteration == 0:
                session = client.post("/sessions", json={}).json()["id"]
                choice = {
                    "backend": "antigravity",
                    "model": "gemini-test",
                    "effort": "low",
                    "permission": "auto_review",
                }
                assert client.put(f"/api/agents/sessions/{session}", json=choice).status_code == 200
            assert client.get(f"/health?session_id={session}").json()["provider"] == "antigravity"
            result = client.post("/chat", json={"session_id": session, "message": "测甲醇"})
            assert result.status_code == 200, result.text
            body = result.json()
            assert body["answer"] == "甲醇 CH4O"
            assert body["tool_calls"][0]["result"]["formula"] == "CH4O"
            assert body["usage"]["total_tokens"] == 0  # occupancy is NOT billing
            assert body["context"]["input_tokens_after"] == 60
            assert (
                client.get(f"/api/agents/sessions/{session}").json()["external_thread_id"]
                == "native-session"
            )
    assert [method for method, _ in fake_acp.requests].count("session/new") == 1
    assert [method for method, _ in fake_acp.requests].count("session/load") == 1
    assert any(p.get("configId") == "mode" and p["value"] == "auto" for _, p in fake_acp.requests)


@pytest.mark.asyncio
async def test_acp_uncertain_delivery_does_not_replay(settings_factory, fake_acp, tmp_path):
    from chemistry_agent.agent import RunProgress
    from chemistry_agent.backends.service import BackendRouter, BackendSettings
    from chemistry_agent.runtime.checkpoint import Checkpoint, current_checkpoint
    from chemistry_agent.store import Store
    from chemistry_agent.tools import ToolRegistry

    store = await Store.open(settings_factory().database_path)
    try:
        router = BackendRouter(
            store,
            None,
            ToolRegistry(),
            tmp_path,
            "",
        )
        await router.initialize()
        await router.save_settings(BackendSettings(default_backend="antigravity"))
        session = (await store.create_session("test"))["id"]
        token = current_checkpoint.set(Checkpoint(store, "stable-input"))
        fake_acp.failure = True
        try:
            for expected in ("disconnect", "不会重复发送"):
                with pytest.raises(AgentError, match=expected):
                    await router.run(
                        [{"role": "user", "content": "hello"}],
                        RunProgress(),
                        session_id=session,
                        run_id="r",
                        history_count=0,
                    )
            assert fake_acp.prompts == 1
        finally:
            current_checkpoint.reset(token)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mcp_rejects_invalid_token_and_cleans_socket():
    executed = []

    async def execute(name, args):
        executed.append(name)
        return {}

    async with PlatformMCPBridge([], execute) as bridge:
        path = bridge.path
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(
            b'{"token":"wrong","message":{"method":"tools/call","params":{"name":"submit"}}}\n'
        )
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    assert not path.exists()
    assert not executed


def test_catalog_does_not_invent_other_model_effort():
    catalog = model_catalog({"configOptions": OPTIONS})
    assert catalog[0]["efforts"] == ["low"]


@pytest.mark.asyncio
@pytest.mark.parametrize("approved,expected", [(True, "yes"), (False, "no")])
async def test_native_permission_is_answered_once(approved, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from chemistry_agent.agent import RunProgress

    requests = []

    async def ask(session, run, payload):
        requests.append((session, run, payload))
        return approved

    async def checkpoint():
        pass

    backend = AntigravityBackend(
        SimpleNamespace(
            context=SimpleNamespace(acp_event=AsyncMock()), approvals=SimpleNamespace(ask=ask)
        ),
        None,
        "",
    )
    rpc = FakeACP("", [], "")
    prompt = asyncio.get_running_loop().create_future()

    async def send(message):
        assert message["result"]["outcome"] == {"outcome": "selected", "optionId": expected}
        prompt.set_result({"stopReason": "end_turn"})

    rpc.send = send
    await rpc.events.put(
        {
            "id": 1,
            "method": "session/request_permission",
            "params": {
                "sessionId": "native",
                "toolCall": {"toolCallId": "t", "title": "create water.xyz"},
                "options": [
                    {"kind": "allow_once", "optionId": "yes"},
                    {"kind": "reject_once", "optionId": "no"},
                ],
            },
        }
    )
    await backend.consume(
        rpc, prompt, "native", "host-session", "run", "user", RunProgress(), [], checkpoint
    )
    assert len(requests) == 1
    assert requests[0][:2] == ("host-session", "run")


def test_side_question_uses_separate_acp_session(settings_factory, fake_acp, monkeypatch):
    from uuid import uuid4

    original = fake_acp.request

    async def respond(self, method, params, timeout=None):  # noqa: ASYNC109
        if method != "session/prompt":
            return await original(self, method, params, timeout)
        assert self.bridge == []
        assert "旁问测试" in params["prompt"][0]["text"]
        await self.update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "独立回答"},
            }
        )
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(fake_acp, "request", respond)
    with TestClient(
        create_app(settings_factory(deepseek_api_key="")),
        base_url="http://localhost",
        client=("127.0.0.1", 5000),
    ) as client:
        session = client.post("/sessions", json={}).json()["id"]
        client.put(
            f"/api/agents/sessions/{session}",
            json={
                "backend": "antigravity",
                "model": "gemini-test",
                "effort": "low",
                "permission": "user",
            },
        ).raise_for_status()
        response = client.post(
            f"/sessions/{session}/side-chat", json={"id": str(uuid4()), "question": "旁问测试"}
        )
        assert response.json()["answer"] == "独立回答"
        assert client.get(f"/sessions/{session}").json()["messages"] == []
        assert client.get(f"/api/agents/sessions/{session}").json()["external_thread_id"] is None
