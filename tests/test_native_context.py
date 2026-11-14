"""Context observations are durable; native maintenance never replays chat/tools."""

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.backends.codex import CodexBackend


def bind(client, backend: str = "codex") -> str:
    session = client.post("/sessions", json={}).json()["id"]
    result = client.put(
        f"/api/agents/sessions/{session}",
        json={
            "backend": backend,
            "permission": "user",
            "model": "",
            "effort": "",
        },
    )
    assert result.status_code == 200, result.text

    async def native() -> None:
        await client.app.state.agent_backends.update_thread(session, external_thread_id="native")

    client.portal.call(native)
    return session


def context(client, session: str) -> dict:
    result = client.get(f"/sessions/{session}/context")
    assert result.status_code == 200
    return result.json()


def settle(client) -> None:
    async def wait() -> None:
        await asyncio.gather(*client.app.state.agent_backends.context.tasks)

    client.portal.call(wait)


def test_usage_is_last_context_not_lifetime_and_survives_restart(settings_factory):
    settings = settings_factory()
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        ledger = client.app.state.agent_backends.context
        client.portal.call(
            ledger.codex_event,
            session,
            "native",
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "native",
                    "tokenUsage": {
                        "total": {"totalTokens": 999999},
                        "last": {"totalTokens": 4200},
                        "modelContextWindow": 10000,
                    },
                },
            },
        )
        client.portal.call(
            ledger.codex_event,
            session,
            "native",
            {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": "wrong", "tokenUsage": {"last": {"totalTokens": 1}}},
            },
        )
        assert context(client, session)["used_tokens"] == 4200
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        assert context(client, session)["used_tokens"] == 4200
        assert context(client, session)["capacity_tokens"] == 10000
        assert "compaction" not in context(client, session)


def test_acp_capabilities_and_missing_fields_are_not_zero(settings_factory):
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client, "antigravity")
        ledger = client.app.state.agent_backends.context
        assert not context(client, session)["manual_supported"]
        client.portal.call(
            ledger.acp_event,
            session,
            "native",
            {
                "method": "session/update",
                "params": {
                    "sessionId": "native",
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [{"name": "compact"}],
                    },
                },
            },
        )
        client.portal.call(
            ledger.acp_event,
            session,
            "native",
            {
                "method": "session/update",
                "params": {
                    "sessionId": "native",
                    "update": {
                        "sessionUpdate": "usage_update",
                        "size": 1000000,
                    },
                },
            },
        )
        info = context(client, session)
        assert info["used_tokens"] is None
        assert info["capacity_tokens"] == 1000000
        assert info["manual_supported"]


class CompactCodex:
    requests = []
    broken = False

    def __init__(self, executable):
        self.events = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, params):
        type(self).requests.append((method, params))
        if method == "thread/compact/start":
            for phase in ["started", "completed"]:
                await self.events.put(
                    {
                        "method": f"item/{phase}",
                        "params": {
                            "threadId": "native",
                            "turnId": "compact-turn",
                            "item": {"id": "compact-item", "type": "contextCompaction"},
                        },
                    }
                )
                if self.broken:
                    await self.events.put({"transport_error": "disconnected"})
                    return {}
            await self.events.put(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "native",
                        "turnId": "compact-turn",
                        "tokenUsage": {"last": {"totalTokens": 120}, "modelContextWindow": 1000},
                    },
                }
            )
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "native",
                        "turn": {"id": "compact-turn", "status": "completed"},
                    },
                }
            )
        return {}


def install_codex(monkeypatch, client):
    monkeypatch.setattr(CodexBackend, "executable", staticmethod(lambda config: "/fake"))
    CompactCodex.requests, CompactCodex.broken = [], False
    client.app.state.agent_backends.codex.transport_factory = CompactCodex


def test_manual_codex_compaction_idempotency_and_original_thread(settings_factory, monkeypatch):
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        install_codex(monkeypatch, client)
        client.portal.call(
            client.app.state.agent_backends.context.usage, session, "codex", "native", 900, 1000
        )
        identifier = str(uuid4())
        url = f"/sessions/{session}/context/compact?operation_id={identifier}"
        assert client.post(url).status_code == 202
        settle(client)
        assert client.post(url).status_code == 202
        info = context(client, session)
        assert info["compaction"]["status"] == "completed"
        assert info["compaction"]["before_tokens"] == 900
        assert info["compaction"]["after_tokens"] == 120
        assert info["used_tokens"] == 120
        assert not info["busy"]
        assert [m for m, _ in CompactCodex.requests] == ["thread/resume", "thread/compact/start"]
        assert CompactCodex.requests[0][1]["threadId"] == "native"
        assert client.get(f"/sessions/{session}").json()["messages"] == []
        run = client.get(f"/runs/{identifier}").json()
        assert run["context"]["manual_compaction"] is True


def test_disconnect_retains_receipt_does_not_retry(settings_factory, monkeypatch):
    settings = settings_factory()
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        install_codex(monkeypatch, client)
        CompactCodex.broken = True
        identifier = str(uuid4())
        url = f"/sessions/{session}/context/compact?operation_id={identifier}"
        assert client.post(url).status_code == 202
        settle(client)
        assert context(client, session)["compaction"]["status"] == "interrupted"
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        assert client.post(url).json()["state"] == "interrupted"
        assert len(CompactCodex.requests) == 2


def test_exclusive_lease_keeps_queued_user_message(settings_factory, monkeypatch):
    async def block(self, session, binding, kind):
        await asyncio.Event().wait()

    monkeypatch.setattr(CodexBackend, "context_operation", block)
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        assert client.post(f"/sessions/{session}/context/compact").status_code == 202
        assert context(client, session)["busy"]
        assert client.post(f"/sessions/{session}/context/compact").status_code == 409
        result = client.put(
            f"/api/agents/sessions/{session}", json={"backend": "builtin", "permission": "full"}
        )
        assert result.status_code == 409
        receipt = client.post(
            "/chat/requests",
            json={
                "session_id": session,
                "request_id": str(uuid4()),
                "message": "稍后处理这条消息",
            },
        )
        assert receipt.status_code == 202
        queued = client.get(f"/sessions/{session}/chat-requests").json()["requests"]
        assert queued[0]["status"] == "queued"
        # Shutdown cancels the maintenance lease; it never manufactures a chat turn.


def test_native_compaction_events_can_be_automatic(settings_factory):
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        ledger = client.app.state.agent_backends.context
        client.portal.call(ledger.usage, session, "codex", "native", 900, 1000)
        for phase in ["started", "completed", "completed"]:
            client.portal.call(
                ledger.codex_event,
                session,
                "native",
                {
                    "method": f"item/{phase}",
                    "params": {
                        "threadId": "native",
                        "item": {"id": "auto", "type": "contextCompaction"},
                    },
                },
            )
        info = context(client, session)
        assert not info["compaction"]["manual"]
        assert info["used_tokens"] is None  # Don't retain a stale percentage after compaction.
        assert "after_tokens" not in info["compaction"]
        client.portal.call(ledger.usage, session, "codex", "native", 200, 1000)
        assert "after_tokens" not in context(client, session)["compaction"]


class CompactACP:
    prompts = []
    advertise = True

    def __init__(self, executable, arguments, cwd):
        self.events = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def update(self, data):
        await self.events.put(
            {"method": "session/update", "params": {"sessionId": "native", "update": data}}
        )

    async def request(self, method, params, timeout=None):  # noqa: ASYNC109
        if method == "session/load":

            async def delayed() -> None:
                await asyncio.sleep(0.01)
                await self.update(
                    {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [{"name": "compact"}] if self.advertise else [],
                    }
                )
                await self.update({"sessionUpdate": "usage_update", "used": 800, "size": 1000})

            asyncio.create_task(delayed())
            return {
                "configOptions": [
                    {"category": "mode", "id": "mode", "options": [{"value": "default"}]}
                ]
            }
        if method == "session/prompt":
            type(self).prompts.append(params)
            await self.update({"sessionUpdate": "usage_update", "used": 100, "size": 1000})
            return {"stopReason": "end_turn"}
        return {}


def test_antigravity_advertised_command_runs_natively(settings_factory, monkeypatch):
    from chemistry_agent.backends.antigravity import AntigravityBackend

    monkeypatch.setattr(AntigravityBackend, "executable", staticmethod(lambda config: "/fake"))
    CompactACP.prompts, CompactACP.advertise = [], True
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client, "antigravity")
        client.app.state.agent_backends.antigravity.transport_factory = CompactACP
        assert client.post(f"/sessions/{session}/context/compact").status_code == 409
        assert (
            client.post(f"/sessions/{session}/context/refresh?operation_id={uuid4()}").status_code
            == 202
        )
        settle(client)
        assert not CompactACP.prompts
        assert context(client, session)["manual_supported"]
        assert client.post(f"/sessions/{session}/context/compact").status_code == 202
        settle(client)
        assert CompactACP.prompts == [
            {"sessionId": "native", "prompt": [{"type": "text", "text": "/compact"}]}
        ]
        info = context(client, session)
        assert info["used_tokens"] == 100
        assert info["compaction"]["status"] == "completed"
        assert info["compaction"]["before_tokens"] == 800
        assert info["compaction"]["after_tokens"] == 100
        # Capability removal is rechecked before dispatching, even with an older cached True.
        CompactACP.advertise = False
        assert client.post(f"/sessions/{session}/context/compact").status_code == 202
        settle(client)
        assert len(CompactACP.prompts) == 1
        assert not context(client, session)["manual_supported"]
        assert context(client, session)["operation"]["state"] == "failed"


def test_model_switch_hides_previous_capacity(settings_factory):
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        client.portal.call(
            client.app.state.agent_backends.context.usage, session, "codex", "native", 900, 1000
        )
        assert (
            client.put(
                f"/api/agents/sessions/{session}",
                json={"backend": "codex", "model": "new", "permission": "user"},
            ).status_code
            == 200
        )
        info = context(client, session)
        assert info["used_tokens"] is None
        assert info["capacity_tokens"] is None


def test_restart_marks_unfinished_compaction_interrupted(settings_factory):
    settings = settings_factory()
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = bind(client)
        client.portal.call(
            client.app.state.agent_backends.context.compaction,
            session,
            "codex",
            "native",
            "running",
        )
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        assert context(client, session)["compaction"]["status"] == "interrupted"
        assert not client.app.state.agent_backends.context.tasks
