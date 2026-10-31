"""Model directory and session selection contracts; no real provider or credentials."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.auth import create_user_account
from chemistry_agent.chatgpt import ChatGPTClient
from chemistry_agent.model_runtime import ModelScope, ProviderRouter, current_model_scope
from chemistry_agent.model_settings import ModelSettings


def config(**overrides) -> dict:
    return {
        "provider": "openai-compatible",
        "base_url": "https://models.test/v1",
        "model": "default-model",
        "api_key": "private-model-key",
    } | overrides


def completion(content: str = "OK", calls: list | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ]
        },
    )


def test_catalog_uses_saved_destination_key_and_filters_upstream_fields(settings_factory):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-a", "name": "Model A", "secret": "private-upstream-metadata"},
                    {"id": "model-a"},
                    {"id": "model-b"},
                    {"id": "bad\nmodel"},
                    {"id": "private-model-key"},
                    {"id": "model-c", "name": "private-model-key"},
                ],
                "debug": "private-model-key",
            },
        )

    app = create_app(settings_factory(), transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        assert client.put("/api/model/settings", json=config()).status_code == 200
        result = client.get("/api/model/models")
        assert result.status_code == 200
        assert result.json() == {
            "provider": "openai-compatible",
            "source": "api",
            "models": [
                {"id": "model-a", "name": "Model A"},
                {"id": "model-b", "name": "model-b"},
                {"id": "model-c", "name": "model-c"},
            ],
        }
        assert str(requests[0].url) == "https://models.test/v1/models"
        assert requests[0].headers["authorization"] == "Bearer private-model-key"
        assert "private-" not in result.text


@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {"error": "private-model-key"}),
        (302, {"error": "private-model-key"}),
        (200, {"unexpected": "private-model-key"}),
        (200, {"data": "private-model-key"}),
    ],
)
def test_catalog_errors_never_echo_upstream_or_follow_redirect(settings_factory, status, payload):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(
            status, json=payload, headers={"Location": "https://other.test/secret"}
        )

    with TestClient(
        create_app(settings_factory(), transport=httpx.MockTransport(respond))
    ) as client:
        client.put("/api/model/settings", json=config())
        result = client.get("/api/model/models")
        assert result.status_code == 502
        assert "private-model-key" not in result.text and "other.test" not in result.text
        assert calls == ["https://models.test/v1/models"]


def test_chatgpt_catalog_and_account_routes(settings_factory, monkeypatch):
    operations = []

    async def list_models(self, user_id):
        operations.append(("models", user_id))
        return [{"id": "account-model", "name": "Account Model"}]

    async def start_login(self, user_id, method, **kwargs):
        operations.append(("login", user_id, method, kwargs))
        return {"state": "waiting"}

    async def select_account(self, user_id, account_id):
        operations.append(("account", user_id, account_id))
        return {"active_account": account_id}

    async def logout(self, user_id):
        return {"remote_revoked": False}

    monkeypatch.setattr(ChatGPTClient, "list_models", list_models, raising=False)
    monkeypatch.setattr(ChatGPTClient, "start_login", start_login)
    monkeypatch.setattr(ChatGPTClient, "select_account", select_account, raising=False)
    monkeypatch.setattr(ChatGPTClient, "logout", logout)
    with TestClient(create_app(settings_factory(deepseek_api_key=None))) as client:
        result = client.get("/api/model/models")
        assert result.json() == {
            "provider": "chatgpt",
            "source": "chatgpt",
            "models": [{"id": "account-model", "name": "Account Model"}],
        }
        assert (
            client.post(
                "/api/model/chatgpt/login", json={"account_id": "account-1", "new_account": True}
            ).status_code
            == 200
        )
        assert operations[-1] == (
            "login",
            None,
            "browser",
            {"account_id": "account-1", "new_account": True},
        )
        assert client.post(
            "/api/model/chatgpt/account", json={"account_id": "account-2"}
        ).json() == {"active_account": "account-2"}
        result = client.post("/api/model/chatgpt/logout")
        assert result.status_code == 200 and result.json() == {"remote_revoked": False}


def test_session_model_persists_and_request_null_means_default(settings_factory):
    models = []

    def respond(request):
        models.append(json.loads(request.content)["model"])
        return completion()

    settings = settings_factory()
    with TestClient(create_app(settings, transport=httpx.MockTransport(respond))) as client:
        client.put("/api/model/settings", json=config())
        session = client.post("/sessions", json={}).json()["id"]
        path = f"/sessions/{session}/model"
        assert client.get(path).json()["model"] is None
        assert client.put(path, json={"model": "chosen-model"}).json()["model"] == "chosen-model"
        for extra in ({}, {"model": "once-only"}, {"model": None}, {}):
            result = client.post("/chat", json={"session_id": session, "message": "hi", **extra})
            assert result.status_code == 200, result.text
        assert models == ["chosen-model", "once-only", "default-model", "chosen-model"]
        assert client.get("/api/model/settings").json()["model"] == "default-model"
        for invalid in ("", "bad\nmodel", "x" * 161):
            assert client.put(path, json={"model": invalid}).status_code == 422
            assert client.post("/chat", json={"message": "hi", "model": invalid}).status_code == 422
    with TestClient(create_app(settings, transport=httpx.MockTransport(respond))) as client:
        assert client.get(path).json()["model"] == "chosen-model"
        assert client.put(path, json={"model": None}).status_code == 200
        assert client.get(path).json()["model"] is None


def test_changed_provider_or_api_destination_does_not_reuse_preference(settings_factory):
    with TestClient(create_app(settings_factory())) as client:
        client.put("/api/model/settings", json=config())
        session = client.post("/sessions", json={}).json()["id"]
        path = f"/sessions/{session}/model"
        client.put(path, json={"model": "old-endpoint-model"})
        client.put("/api/model/settings", json=config(base_url="https://second.test/v1"))
        assert client.get(path).json()["model"] is None
        client.put("/api/model/settings", json={"provider": "chatgpt"})
        assert client.get(path).json() == {"provider": "chatgpt", "model": None}


async def test_owner_and_cross_origin_protection(settings_factory):
    settings = settings_factory(auth_enabled=True, cookie_secure=False)
    await create_user_account(settings, "alice", "password-alice-123")
    await create_user_account(settings, "bobby", "password-bobby-123")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/model/models").status_code == 401
        client.post("/auth/login", json={"username": "alice", "password": "password-alice-123"})
        session = client.post("/sessions", json={}).json()["id"]
        path = f"/sessions/{session}/model"
        assert client.put(path, json={"model": "alice-model"}).status_code == 200
        assert (
            client.put(
                path, json={"model": "bad"}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        client.post("/auth/logout")
        client.post("/auth/login", json={"username": "bobby", "password": "password-bobby-123"})
        assert client.get(path).status_code == 404
        assert client.put(path, json={"model": "bobby-model"}).status_code == 404
        assert (
            client.post(
                "/chat", json={"message": "hi", "session_id": session, "model": "bobby-model"}
            ).status_code
            == 404
        )


def test_compaction_and_tool_rounds_keep_request_model_snapshot(settings_factory, monkeypatch):
    seen = []
    app = None

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["model"])
        if len(seen) == 1:
            # The summary request was sent; settings change before the main and tool rounds.
            app.state.provider.store.save(None, ModelSettings(**config(model="next-default")))
            return completion("summary")
        if len(seen) == 2:
            return completion(
                "",
                [
                    {
                        "id": "tool-1",
                        "type": "function",
                        "function": {
                            "name": "molecular_properties",
                            "arguments": '{"smiles":"CCO"}',
                        },
                    }
                ],
            )
        return completion("done")

    app = create_app(settings_factory(), transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        client.put("/api/model/settings", json=config())
        manager = app.state.context_manager
        original = manager.prepare
        summarized = False

        async def prepare(*args, **kwargs):
            nonlocal summarized
            if not summarized:
                summarized = True
                progress = kwargs.get("progress")
                if progress is None:
                    # Progress is the final positional argument in ContextManager.prepare.
                    progress = args[-1]
                await manager._summarize("", "one past user turn", progress)
            return await original(*args, **kwargs)

        monkeypatch.setattr(manager, "prepare", prepare)
        result = client.post("/chat", json={"message": "计算乙醇", "model": "frozen-model"})
        assert result.status_code == 200, result.text
        assert seen == ["frozen-model", "frozen-model", "frozen-model"]
        assert result.json()["tool_calls"][0]["success"]
        assert client.get("/api/model/settings").json()["model"] == "next-default"


async def test_concurrent_scopes_do_not_mutate_each_other(settings_factory):
    router = ProviderRouter(settings_factory())

    async def request(model: str) -> None:
        token = current_model_scope.set(ModelScope(user_id=None))
        try:
            original = router.configuration()[1].model
            router.select_model(model)
            await asyncio.sleep(0)
            assert router.configuration()[1].model == model
            router.select_model(None)
            assert router.configuration()[1].model == original
        finally:
            current_model_scope.reset(token)

    try:
        await asyncio.gather(request("model-a"), request("model-b"))
    finally:
        await router.close()
