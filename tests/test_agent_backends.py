"""Backend switching, tool bridge, restart continuity and no-replay acceptance."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.agent import RunProgress
from chemistry_agent.api import create_app
from chemistry_agent.backends.codex import CodexBackend
from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.live import LiveAnswer


class FakeCodex:
    requests = []
    replies = []
    starts = 0
    failure = False

    def __init__(self, executable: str) -> None:
        self.events = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        pass

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "external-thread"}}
        if method == "thread/resume":
            assert params["threadId"] == "external-thread"
            return {"thread": {"id": "external-thread"}}
        if method == "turn/start":
            type(self).starts += 1
            if self.failure:
                await self.events.put({"transport_error": "disconnect"})
            else:
                await self.events.put(
                    {
                        "id": "request1",
                        "method": "item/tool/call",
                        "params": {
                            "threadId": "external-thread",
                            "turnId": "turn1",
                            "callId": "tool1",
                            "tool": "molecular_properties",
                            "arguments": {"smiles": "CO"},
                        },
                    }
                )
            return {"turn": {"id": "turn1"}}
        return {}

    async def send(self, message: dict) -> None:
        self.replies.append(message)
        assert message["result"]["success"]
        assert "CH4O" in message["result"]["contentItems"][0]["text"]
        await self.events.put(
            {"method": "item/agentMessage/delta", "params": {"delta": "甲醇 CH4O"}}
        )
        await self.events.put(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "total": {"inputTokens": 110, "outputTokens": 55, "totalTokens": 165},
                        "last": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                    }
                },
            }
        )
        await self.events.put(
            {"method": "turn/completed", "params": {"turn": {"id": "turn1", "status": "completed"}}}
        )


@pytest.fixture
def fake_codex(monkeypatch):
    FakeCodex.requests, FakeCodex.replies, FakeCodex.starts = [], [], 0
    FakeCodex.failure = False
    original = CodexBackend.__init__

    def initialize(self, *args):
        original(self, *args)
        self.transport_factory = FakeCodex

    monkeypatch.setattr(CodexBackend, "__init__", initialize)
    monkeypatch.setattr(CodexBackend, "executable", staticmethod(lambda config: "/fake/codex"))
    return FakeCodex


def no_provider(request):
    pytest.fail("External Agent must not call the built-in model provider")


async def test_codex_commentary_leaves_live_preview_once_recorded():
    preview = LiveAnswer()
    steps = []
    router = SimpleNamespace(context=SimpleNamespace(codex_event=AsyncMock()))
    backend = CodexBackend(router, None, "")

    async def updates():
        commentary = {"id": "note", "type": "agentMessage", "phase": "commentary"}
        yield {"method": "item/started", "params": {"item": commentary}}
        yield {"method": "item/agentMessage/delta", "params": {"delta": "Checking records."}}
        assert preview.text == "Checking records."
        yield {
            "method": "item/completed",
            "params": {"item": commentary | {"text": "Checking records."}},
        }
        assert preview.steps == [{"type": "commentary", "text": "Checking records."}]
        assert preview.text == ""
        yield {"method": "item/started", "params": {"item": {"type": "agentMessage"}}}
        yield {"method": "item/agentMessage/delta", "params": {"delta": "Records verified."}}
        assert preview.text == "Records verified."
        yield {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn", "status": "completed"}},
        }

    rpc = SimpleNamespace(events=SimpleNamespace(get=updates().__anext__))
    answer = await backend.consume(
        rpc, "thread", "turn", "session", "run", RunProgress(preview=preview), steps, [], []
    )
    assert answer == "Records verified."


def test_local_codex_tools_resume_and_settings(settings_factory, fake_codex):
    settings = settings_factory(deepseek_api_key="")
    for iteration in range(2):
        with TestClient(
            create_app(settings, transport=httpx.MockTransport(no_provider)),
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ) as client:
            if iteration == 0:
                old = client.post("/sessions", json={}).json()["id"]
                assert (
                    client.put(
                        "/api/agents/settings", json={"default_backend": "codex"}
                    ).status_code
                    == 200
                )
                session = client.post("/sessions", json={}).json()["id"]
                assert client.get(f"/api/agents/sessions/{old}").json()["backend"] == "builtin"
            assert client.get(f"/health?session_id={session}").json()["provider"] == "codex"
            result = client.post("/chat", json={"session_id": session, "message": "计算甲醇性质"})
            assert result.status_code == 200, result.text
            assert result.json()["answer"] == "甲醇 CH4O"
            assert result.json()["usage"]["total_tokens"] == 15
            assert result.json()["tool_calls"][0]["result"]["formula"] == "CH4O"
            binding = client.get(f"/api/agents/sessions/{session}").json()
            assert binding["external_thread_id"] == "external-thread"
            assert binding["turn_state"] == "completed"
            assert client.post(f"/sessions/{session}/title").status_code == 200
            context = client.get(f"/sessions/{session}/context").json()
            assert context["used_tokens"] == 15
            assert context["capacity_tokens"] is None
            assert context["manual_supported"] is True
    assert [m for m, _ in fake_codex.requests].count("thread/start") == 1
    assert [m for m, _ in fake_codex.requests].count("thread/resume") == 1
    tools = next(p["dynamicTools"] for m, p in fake_codex.requests if m == "thread/start")
    assert {"memory_read", "memory_save", "ask_user_question"} <= {t["name"] for t in tools}


@pytest.mark.asyncio
async def test_delivery_receipt_replay_and_uncertain(settings_factory, fake_codex):
    from chemistry_agent.agent import RunProgress
    from chemistry_agent.backends.service import BackendRouter, BackendSettings
    from chemistry_agent.runtime.checkpoint import Checkpoint, current_checkpoint
    from chemistry_agent.store import Store
    from chemistry_agent.tools import ToolRegistry

    settings = settings_factory()
    store = await Store.open(settings.database_path)
    try:
        router = BackendRouter(store, None, ToolRegistry(), settings.database_path.parent, "")
        await router.initialize()
        await router.save_settings(BackendSettings(default_backend="codex"))
        session = (await store.create_session("test"))["id"]
        token = current_checkpoint.set(Checkpoint(store, "stable-input"))
        try:
            kwargs = {"session_id": session, "run_id": "r1", "history_count": 0}
            messages = [{"role": "user", "content": "甲醇"}]
            await router.run(messages, RunProgress(), **kwargs)
            await router.run(messages, RunProgress(), **{**kwargs, "run_id": "r2"})
            assert fake_codex.starts == 1
        finally:
            current_checkpoint.reset(token)
        fake_codex.failure = True
        token = current_checkpoint.set(Checkpoint(store, "disconnect-input"))
        try:
            with pytest.raises(AgentError, match="Codex 中断"):
                await router.run(messages, RunProgress(), **kwargs)
            with pytest.raises(AgentError, match="不会重复发送"):
                await router.run(messages, RunProgress(), **kwargs)
            assert fake_codex.starts == 2
        finally:
            current_checkpoint.reset(token)
    finally:
        await store.close()


def test_backend_settings_are_local_only(settings_factory, fake_codex):
    with TestClient(create_app(settings_factory()), base_url="http://untrusted.test") as client:
        assert (
            client.put("/api/agents/settings", json={"default_backend": "codex"}).status_code == 403
        )
        assert client.post("/api/agents/codex/probe", json={}).status_code == 403


@pytest.mark.parametrize(
    "permission,policy,reviewer,sandbox",
    [
        ("user", "on-request", "user", "workspace-write"),
        ("auto_review", "on-request", "auto_review", "workspace-write"),
        ("full", "never", "user", "danger-full-access"),
    ],
)
def test_session_choice_reaches_codex_and_survives_reload(
    settings_factory, fake_codex, permission, policy, reviewer, sandbox
):
    settings = settings_factory()
    app = create_app(settings, transport=httpx.MockTransport(no_provider))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        session = client.post("/sessions", json={}).json()["id"]
        choice = {
            "backend": "codex",
            "model": "test-model",
            "effort": "high",
            "permission": permission,
        }
        response = client.put(f"/api/agents/sessions/{session}", json=choice)
        assert response.status_code == 200, response.text
        assert {key: response.json()[key] for key in choice} == choice
        assert response.json()["host_id"] == "local"
        assert (
            client.post("/chat", json={"session_id": session, "message": "甲醇"}).status_code == 200
        )
        params = next(p for method, p in fake_codex.requests if method == "thread/start")
        assert (params["approvalPolicy"], params["approvalsReviewer"], params["sandbox"]) == (
            policy,
            reviewer,
            sandbox,
        )
        assert params["model"] == "test-model"
        assert (
            next(p for method, p in fake_codex.requests if method == "turn/start")["effort"]
            == "high"
        )
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        data = client.get(f"/api/agents/sessions/{session}").json()
        assert all(data[key] == value for key, value in choice.items())
        # Changing consumers starts with platform history, not a stale native thread.
        data = client.put(
            f"/api/agents/sessions/{session}",
            json={
                "backend": "builtin",
                "model": "chem-model",
                "effort": "enabled",
                "permission": "full",
            },
        )
        assert data.status_code == 200, data.text
        assert client.get(f"/api/agents/sessions/{session}").json()["external_thread_id"] is None
        assert client.get(f"/sessions/{session}/model").json()["model"] == "chem-model"


@pytest.mark.asyncio
async def test_approval_is_run_bound_single_use_and_expires(settings_factory):
    from chemistry_agent.backends.approvals import ApprovalBroker
    from chemistry_agent.store import Store

    store = await Store.open(settings_factory().database_path)
    broker = ApprovalBroker(store)
    async with store.transaction():
        await broker.initialize()
    try:
        task = asyncio.create_task(
            broker.ask("s1", "run1", {"title": "test", "details": {"command": "echo test"}})
        )
        for _ in range(50):
            rows = await broker.list_pending("s1")
            if rows:
                break
            await asyncio.sleep(0.01)
        assert rows[0]["run_id"] == "run1"
        with pytest.raises(AgentError):
            await broker.resolve("another-session", rows[0]["id"], True)
        assert not task.done()
        await broker.resolve("s1", rows[0]["id"], False)
        assert await task is False
        with pytest.raises(AgentError):
            await broker.resolve("s1", rows[0]["id"], True)
        task = asyncio.create_task(broker.ask("s1", "run2", {"title": "test"}))
        for _ in range(50):
            if await broker.list_pending("s1"):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await broker.list_pending("s1") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_choice_cannot_change_active_run(settings_factory, fake_codex):
    from chemistry_agent.backends.service import BackendRouter, SessionSelection
    from chemistry_agent.model_runtime import ModelScope, ProviderRouter, current_model_scope
    from chemistry_agent.store import Store
    from chemistry_agent.tools import ToolRegistry

    settings = settings_factory()
    store = await Store.open(settings.database_path)
    provider = ProviderRouter(settings)
    token = current_model_scope.set(ModelScope(None))
    try:
        router = BackendRouter(store, None, ToolRegistry(), settings.database_path.parent, "")
        await router.initialize()
        session = (await store.create_session("busy"))["id"]
        await store.begin_run(session)
        choice = SessionSelection(backend="codex", model="changed", permission="full")
        with pytest.raises(AgentError, match="本轮结束"):
            await router.select(session, choice, provider)
        assert (await router.binding(session))["backend"] == "builtin"
    finally:
        current_model_scope.reset(token)
        await provider.close()
        await store.close()


@pytest.mark.asyncio
async def test_native_approval_roundtrip(settings_factory, fake_codex):
    from chemistry_agent.agent import RunProgress
    from chemistry_agent.backends.service import BackendRouter
    from chemistry_agent.store import Store
    from chemistry_agent.tools import ToolRegistry

    settings = settings_factory()
    store = await Store.open(settings.database_path)
    router = BackendRouter(
        store,
        None,
        ToolRegistry(),
        settings.database_path.parent,
        "",
    )
    await router.initialize()

    class Connection:
        def __init__(self) -> None:
            self.events = asyncio.Queue()
            self.response = None

        async def send(self, message: dict) -> None:
            self.response = message
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "t1", "status": "completed"}},
                }
            )

    connection = Connection()
    await connection.events.put(
        {
            "id": 12,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "th1", "turnId": "t1", "command": "echo test", "reason": "test"},
        }
    )
    task = asyncio.create_task(
        router.codex.consume(connection, "th1", "t1", "s1", "r1", RunProgress(), [], [], [])
    )
    try:
        for _ in range(50):
            rows = await router.approvals.list_pending("s1")
            if rows:
                break
            await asyncio.sleep(0.01)
        assert not task.done()
        assert rows[0]["details"]["command"] == "echo test"
        await router.approvals.resolve("s1", rows[0]["id"], True)
        assert await task == ""
        assert connection.response == {"id": 12, "result": {"decision": "accept"}}
    finally:
        task.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_chem_tool_approval_prevents_denied_effect(settings_factory):
    from chemistry_agent.backends.approvals import ApprovalBroker, current_tool_approval
    from chemistry_agent.provider import ToolCall
    from chemistry_agent.store import Store
    from chemistry_agent.tools import MoleculeInput, Tool, ToolContext, ToolRegistry

    store = await Store.open(settings_factory().database_path)
    broker = ApprovalBroker(store)
    async with store.transaction():
        await broker.initialize()
    effects = []
    registry = ToolRegistry(
        [
            Tool(
                "write_test",
                "test",
                MoleculeInput,
                lambda args, context: effects.append(args.smiles),
                contextual=True,
            )
        ]
    )
    token = current_tool_approval.set(broker)
    task = asyncio.create_task(
        registry.execute(
            ToolCall(
                id="call",
                type="function",
                function={"name": "write_test", "arguments": '{"smiles":"O"}'},
            ),
            ToolContext("s1", "r1"),
        )
    )
    try:
        for _ in range(50):
            rows = await broker.list_pending("s1")
            if rows:
                break
            await asyncio.sleep(0.01)
        assert not effects
        await broker.resolve("s1", rows[0]["id"], False)
        result = await task
        assert not result.success and not effects
        assert result.result["error"] == "tool_declined"
    finally:
        current_tool_approval.reset(token)
        await store.close()


@pytest.mark.asyncio
async def test_preflight_failure_retries_original_delivery_without_replaying_sent_turn(
    settings_factory, fake_codex, monkeypatch
):
    from chemistry_agent.agent import RunProgress
    from chemistry_agent.backends.service import BackendRouter, BackendSettings
    from chemistry_agent.runtime.checkpoint import Checkpoint, current_checkpoint
    from chemistry_agent.store import Store
    from chemistry_agent.tools import ToolRegistry

    settings = settings_factory()
    store = await Store.open(settings.database_path)
    try:
        router = BackendRouter(store, None, ToolRegistry(), settings.database_path.parent, "")
        await router.initialize()
        await router.save_settings(BackendSettings(default_backend="codex"))
        session = (await store.create_session("retry"))["id"]
        await router.binding(session, create=True)
        await router.update_thread(
            session, external_thread_id="external-thread", turn_state="completed"
        )
        original = fake_codex.request
        fail_once = True

        async def fail_resume(self, method, params):
            nonlocal fail_once
            if method == "thread/resume" and fail_once:
                fail_once = False
                raise AgentError("codex_session_busy", "会话占用", 409)
            return await original(self, method, params)

        monkeypatch.setattr(fake_codex, "request", fail_resume)
        token = current_checkpoint.set(Checkpoint(store, "same-delivery"))
        try:
            kwargs = {"session_id": session, "run_id": "retry1", "history_count": 0}
            messages = [{"role": "user", "content": "甲醇"}]
            with pytest.raises(AgentError, match="占用"):
                await router.run(messages, RunProgress(), **kwargs)
            row = await store._one(
                "SELECT state FROM backend_deliveries WHERE id=?", ("Checkpoint:same-delivery",)
            )
            assert row["state"] == "not_sent"
            assert fake_codex.starts == 0
            await router.run(messages, RunProgress(), **{**kwargs, "run_id": "retry2"})
            await router.run(messages, RunProgress(), **{**kwargs, "run_id": "retry3"})
            assert fake_codex.starts == 1
        finally:
            current_checkpoint.reset(token)
    finally:
        await store.close()


def test_steer_targets_live_codex_turn_and_side_chat_uses_another_thread(
    settings_factory, fake_codex, monkeypatch
):
    from uuid import uuid4

    original = fake_codex.request

    async def controlled(self, method, params):
        if method == "thread/start":
            self.side = bool(params.get("ephemeral"))
            if self.side:
                assert params["sandbox"] == "read-only"
                assert "dynamicTools" not in params
                return {"thread": {"id": "aside"}}
        if method == "turn/start":
            if not getattr(self, "side", False):
                return {"turn": {"id": "main-turn"}}
            assert params["threadId"] == "aside"
            assert "parent prompt" in params["input"][0]["text"]
        elif method == "turn/steer":
            assert params["threadId"] == "external-thread"
            assert params["expectedTurnId"] == "main-turn"
            assert params["input"] == [{"type": "text", "text": "change direction"}]
        else:
            return await original(self, method, params)
        turn_id = "aside-turn" if self.side else "main-turn"
        await self.events.put(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "aside answer" if self.side else "guided answer",
                    }
                },
            }
        )
        await self.events.put(
            {"method": "turn/completed", "params": {"turn": {"id": turn_id, "status": "completed"}}}
        )
        return {"turn": {"id": turn_id}}

    monkeypatch.setattr(fake_codex, "request", controlled)
    app = create_app(settings_factory(), transport=httpx.MockTransport(no_provider))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        ready = asyncio.Event()
        consume = app.state.agent_backends.codex.consume

        async def signal_ready(*args):
            ready.set()
            return await consume(*args)

        monkeypatch.setattr(app.state.agent_backends.codex, "consume", signal_ready)
        client.put("/api/agents/settings", json={"default_backend": "codex"}).raise_for_status()
        sid = client.post("/sessions", json={}).json()["id"]
        req = str(uuid4())
        client.post(
            "/chat/requests",
            json={"request_id": req, "session_id": sid, "message": "parent prompt"},
        ).raise_for_status()

        client.portal.call(asyncio.wait_for, ready.wait(), 5)
        side = client.post(
            f"/sessions/{sid}/side-chat", json={"id": str(uuid4()), "question": "What is ps?"}
        )
        assert side.json()["answer"] == "aside answer"
        path = f"/sessions/{sid}/chat-requests"
        run = client.get(path).json()["active_run_id"]
        reply = client.put(
            f"{path}/{req}/guidance/{uuid4()}", json={"run_id": run, "message": "change direction"}
        )
        assert reply.json() == {"status": "delivered"}

        async def finish():
            await asyncio.gather(*app.state.chat_queue.tasks)

        client.portal.call(finish)
        transcript = client.get(f"/sessions/{sid}").json()["messages"]
        assert transcript[-1]["content"] == "guided answer"
        assert "aside answer" not in str(transcript)
        assert client.get(path).json()["requests"][0]["guidance"][0]["status"] == "delivered"
