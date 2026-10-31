import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.auth import create_user_account
from chemistry_agent.chatgpt import ChatGPTClient, event_usage, response_schema
from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ModelScope, ProviderRouter, current_model_scope
from chemistry_agent.model_settings import ModelSettings, ModelSettingsStore
from chemistry_agent.provider import AssistantMessage, Completion
from chemistry_agent.schemas import TokenUsage


def api_configuration(**changes: object) -> dict:
    return {
        "provider": "openai-compatible",
        "model": "test-model",
        "base_url": "https://model.test/v1",
        "api_key": "private-test-key",
    } | changes


def answer(content: str = "OK", calls: list | None = None) -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
    }


def test_settings_persist_without_exposing_keys_and_changes_apply(settings_factory):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert "thinking" not in payload
        assert payload["model"] == "test-model"
        return httpx.Response(200, json=answer())

    settings = settings_factory(deepseek_api_key=None)
    with TestClient(create_app(settings, transport=httpx.MockTransport(respond))) as client:
        assert not client.get("/health").json()["model_configured"]
        result = client.put("/api/model/settings", json=api_configuration())
        assert result.status_code == 200
        assert "private-test-key" not in result.text
        assert result.json()["has_api_key"]
        assert client.get("/health").json()["model_configured"]
        result = client.post("/api/model/test")
        assert result.status_code == 200, result.text
        assert result.json()["answer"] == "OK"
        assert str(requests[-1].url) == "https://model.test/v1/chat/completions"
        assert requests[-1].headers["authorization"] == "Bearer private-test-key"
        assert client.get("/settings").status_code == 200
        for name in ("settings.css", "settings.mjs"):
            assert client.get(f"/assets/{name}").status_code == 200
        assert (
            client.put("/api/model/settings", json=api_configuration(api_key="")).status_code == 200
        )
        assert client.get("/api/model/settings").json()["has_api_key"]
        assert (
            client.put(
                "/api/model/settings",
                json=api_configuration(
                    base_url="https://different.test/v1",
                    api_key=None,
                ),
            ).status_code
            == 422
        )
    private = ModelSettingsStore(settings).directory(None)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE((private / "settings.json").stat().st_mode) == 0o600
    with TestClient(create_app(settings, transport=httpx.MockTransport(respond))) as client:
        assert client.get("/api/model/settings").json()["has_api_key"]
        assert client.post("/chat", json={"message": "你好"}).status_code == 200
        assert client.delete("/api/model/settings/key").status_code == 204
        assert not client.get("/api/model/settings").json()["has_api_key"]
        assert not client.get("/health").json()["model_configured"]


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": "private-test-key\nsecret"},
        {"api_key": {"secret": "private-test-key"}},
        {"base_url": "http://remote.test/v1"},
        {"base_url": "https://user:private-test-key@example.com/v1"},
        {"base_url": "https://example.com/v1?key=private-test-key"},
        {"provider": "unknown"},
    ],
)
def test_invalid_settings_never_echo_secret(settings_factory, changes):
    with TestClient(create_app(settings_factory())) as client:
        result = client.put("/api/model/settings", json=api_configuration(**changes))
        assert result.status_code == 422
        assert "private-test-key" not in result.text


def test_settings_require_same_origin_and_local_or_authenticated_access(settings_factory):
    app = create_app(settings_factory())
    with TestClient(app) as client:
        for headers in ({"origin": "https://attacker.test"}, {"sec-fetch-site": "cross-site"}):
            assert (
                client.put(
                    "/api/model/settings", json=api_configuration(), headers=headers
                ).status_code
                == 403
            )
        assert (
            client.get("/api/model/settings", headers={"host": "attacker.test"}).status_code == 403
        )
    with TestClient(create_app(settings_factory(auth_enabled=True))) as client:
        assert client.get("/api/model/settings").status_code == 401
        assert client.post("/api/model/chatgpt/login", json={}).status_code == 401


def test_two_accounts_have_independent_settings_and_requests(settings_factory):
    settings = settings_factory(auth_enabled=True, deepseek_api_key=None)
    asyncio.run(create_user_account(settings, "alice", "alice-password-123"))
    asyncio.run(create_user_account(settings, "bobby", "bobby-password-123"))
    keys = []

    def respond(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["authorization"])
        return httpx.Response(200, json=answer())

    with TestClient(create_app(settings, transport=httpx.MockTransport(respond))) as client:
        client.post("/auth/login", json={"username": "alice", "password": "alice-password-123"})
        assert client.put("/api/model/settings", json=api_configuration()).status_code == 200
        client.post("/api/model/test")
        client.post("/auth/logout")
        client.post("/auth/login", json={"username": "bobby", "password": "bobby-password-123"})
        assert not client.get("/api/model/settings").json()["has_api_key"]
        client.put("/api/model/settings", json=api_configuration(api_key="bob-key"))
        client.post("/api/model/test")
        client.post("/auth/logout")
        client.post("/auth/login", json={"username": "alice", "password": "alice-password-123"})
        client.post("/api/model/test")
    assert keys == ["Bearer private-test-key", "Bearer bob-key", "Bearer private-test-key"]


async def test_request_snapshot_survives_settings_change_and_concurrency(settings_factory):
    router = ProviderRouter(settings_factory(deepseek_api_key=None))
    for user in ("alice", "bob"):
        router.store.save(user, ModelSettings(**api_configuration(api_key=user)))

    async def request(user: str) -> None:
        token = current_model_scope.set(ModelScope(user))
        try:
            assert router.configuration()[1].api_key.get_secret_value() == user
            router.store.save(user, ModelSettings(**api_configuration(api_key="updated")))
            await asyncio.sleep(0)
            assert router.configuration()[1].api_key.get_secret_value() == user
        finally:
            current_model_scope.reset(token)

    await asyncio.gather(request("alice"), request("bob"))


def test_local_compatible_model_can_omit_key(settings_factory):
    def respond(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=answer())

    with TestClient(
        create_app(settings_factory(), transport=httpx.MockTransport(respond))
    ) as client:
        response = client.put(
            "/api/model/settings",
            json=api_configuration(
                base_url="http://127.0.0.1:11434/v1",
                api_key=None,
            ),
        )
        assert response.status_code == 200
        assert client.post("/api/model/test").status_code == 200


def test_chatgpt_adapter_executes_existing_tools_via_agent(settings_factory, monkeypatch):
    rounds = []

    async def authenticated(self, user_id):
        return True

    async def complete(self, user_id, configuration, messages, tools, **options):
        rounds.append(messages)
        if len(rounds) == 1:
            assert any(tool["function"]["name"] == "molecular_properties" for tool in tools)
            message = AssistantMessage.model_validate(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "chemistry-1",
                            "type": "function",
                            "function": {
                                "name": "molecular_properties",
                                "arguments": '{"smiles":"CCO"}',
                            },
                        }
                    ],
                }
            )
        else:
            result = json.loads(messages[-1]["content"])
            assert result["formula"] == "C2H6O"
            message = AssistantMessage(role="assistant", content="乙醇分子式 C2H6O，已由工具核实。")
        return Completion(message=message, usage=TokenUsage())

    monkeypatch.setattr(ChatGPTClient, "authenticated", authenticated)
    monkeypatch.setattr(ChatGPTClient, "complete", complete)
    with TestClient(create_app(settings_factory(deepseek_api_key=None))) as client:
        assert client.put("/api/model/settings", json={"provider": "chatgpt"}).status_code == 200
        result = client.post("/chat", json={"message": "计算乙醇 CCO"})
        assert result.status_code == 200, result.text
        assert result.json()["tool_calls"][0]["result"]["formula"] == "C2H6O"
        assert len(rounds) == 2


class FakeProcess:
    def __init__(self, output: Path | None = None, response: dict | None = None):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.output = output
        self.response = response
        self.terminated = False

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, None]:
        if self.output:
            assert b"messages" in data
            self.output.write_text(json.dumps(self.response))
        self.returncode = 0
        return b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":7}}', None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


async def test_codex_subprocess_contract_and_response_schema(settings_factory, monkeypatch):
    client = ChatGPTClient(ModelSettingsStore(settings_factory()))
    recorded = []

    async def authenticated(user_id):
        return True

    async def process(user_id, *arguments, **options):
        recorded.extend(arguments)
        schema_path = Path(arguments[arguments.index("--output-schema") + 1])
        schema = json.loads(await asyncio.to_thread(schema_path.read_text))
        assert schema["additionalProperties"] is False
        assert "reasoning_content" not in schema["properties"]
        return FakeProcess(
            Path(arguments[arguments.index("--output-last-message") + 1]),
            {"role": "assistant", "content": "OK", "tool_calls": []},
        )

    monkeypatch.setattr(client, "authenticated", authenticated)
    monkeypatch.setattr(client, "process", process)
    result = await client.complete(
        None, ModelSettings(), [{"role": "user", "content": "你好"}], [], allow_tools=False
    )
    assert result.message.content == "OK"
    assert result.usage.total_tokens == 19
    assert "--ignore-user-config" in recorded
    assert "read-only" in recorded
    assert "shell_tool" in recorded
    assert "multi_agent" in recorded
    assert "--dangerously-bypass-approvals-and-sandbox" not in recorded
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    monkeypatch.setenv("CODEX_HOME", "/unrelated-desktop-home")
    environment = client.environment("alice")
    assert "OPENAI_API_KEY" not in environment
    assert environment["CODEX_HOME"] != "/unrelated-desktop-home"
    assert environment["CODEX_HOME"] != client.environment("bob")["CODEX_HOME"]


async def test_login_parses_only_official_url_and_code_and_cancels(settings_factory, monkeypatch):
    client = ChatGPTClient(ModelSettingsStore(settings_factory()))
    process = FakeProcess()
    process.stdout.feed_data(
        b"unrelated secret log\nhttps://attacker.test/callback\n"
        b"https://auth.openai.com/codex/device\nABCD-EFGHI\n"
    )

    async def start(user_id, *arguments, **options):
        assert arguments == ("login", "--device-auth")
        return process

    monkeypatch.setattr(client, "process", start)
    monkeypatch.setattr(client, "executable", lambda: "codex")
    await client.start_login("alice", "device")
    await asyncio.sleep(0)
    public = client.attempts["alice"].public()
    assert public == {
        "state": "waiting",
        "url": "https://auth.openai.com/codex/device",
        "code": "ABCD-EFGHI",
    }
    assert "secret" not in json.dumps(public)
    await client.cancel_login("alice")
    assert process.terminated
    assert client.attempts["alice"].public() == {"state": "cancelled", "url": None, "code": None}


async def test_codex_timeout_terminates_process(settings_factory, monkeypatch):
    client = ChatGPTClient(ModelSettingsStore(settings_factory(codex_timeout_seconds=0.01)))
    process = FakeProcess()

    async def hang(data):
        await asyncio.sleep(10)

    async def start(*arguments, **options):
        return process

    async def authenticated(user_id):
        return True

    monkeypatch.setattr(process, "communicate", hang)
    monkeypatch.setattr(client, "authenticated", authenticated)
    monkeypatch.setattr(client, "process", start)
    with pytest.raises(AgentError, match="超时"):
        await client.complete(None, ModelSettings(), [], [], allow_tools=False)
    assert process.terminated


def test_schema_and_usage_tolerate_non_json_log_lines():
    schema = response_schema()
    assert set(schema["properties"]) == set(schema["required"])
    assert (
        event_usage(
            b'not json\n{"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":3}}'
        ).total_tokens
        == 5
    )


def test_deepseek_requires_key_even_for_local_proxy(settings_factory):
    with TestClient(create_app(settings_factory(deepseek_api_key=None))) as client:
        result = client.put(
            "/api/model/settings",
            json=api_configuration(
                provider="deepseek",
                base_url="http://localhost:9999",
                api_key=None,
            ),
        )
        assert result.status_code == 422
        assert result.json()["error"]["code"] == "key_required"


@pytest.mark.parametrize(
    "response",
    [
        {"role": "assistant", "content": "", "tool_calls": []},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        },
    ],
)
async def test_codex_rejects_empty_answers_and_unregistered_tools(
    settings_factory,
    monkeypatch,
    response,
):
    client = ChatGPTClient(ModelSettingsStore(settings_factory()))

    async def authenticated(user_id):
        return True

    async def process(user_id, *arguments, **options):
        return FakeProcess(
            Path(arguments[arguments.index("--output-last-message") + 1]),
            response,
        )

    monkeypatch.setattr(client, "authenticated", authenticated)
    monkeypatch.setattr(client, "process", process)
    with pytest.raises(AgentError) as error:
        await client.complete(None, ModelSettings(), [], [], allow_tools=False)
    assert error.value.code == "provider_invalid_response"


def test_missing_codex_is_actionable_without_blocking_api_mode(settings_factory):
    with TestClient(create_app(settings_factory(codex_binary="/missing/codex"))) as client:
        assert client.get("/health").json()["model_configured"]
        assert client.get("/api/model/chatgpt/status").json()["available"]
        assert not client.get("/api/model/chatgpt/status").json()["legacy_available"]
        result = client.post("/api/model/chatgpt/login", json={"method": "device"})
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "codex_not_installed"
