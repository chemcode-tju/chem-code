import asyncio
import json
import stat
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from chemistry_agent.chatgpt import ChatGPTClient, LoginAttempt
from chemistry_agent.chatgpt_plan import (
    ISSUER,
    PLAN_SCOPE,
    ChatGPTPlan,
    authorization_url,
    token_record,
    verify_identity,
)
from chemistry_agent.chatgpt_responses import PlanResponses
from chemistry_agent.errors import AgentError
from chemistry_agent.model_settings import ModelSettingsStore


@pytest.fixture
def signing():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    public.update(kid="test-key", alg="RS256", use="sig")

    def sign(**changes):
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": "oaiapp_test",
                "sub": "subject-test",
                "exp": int(time.time()) + 3600,
                "nonce": "nonce-test",
            }
            | changes,
            private,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    return sign, {"keys": [public]}


def test_identity_validation_rejects_wrong_bindings_and_signatures(signing):
    sign, keys = signing
    assert verify_identity(sign(), keys, "oaiapp_test", nonce="nonce-test")["sub"] == "subject-test"
    for changes in (
        {"iss": "https://attacker.test"},
        {"aud": "other"},
        {"exp": 0},
        {"nonce": "wrong"},
        {"sub": "other"},
        {"azp": "other"},
    ):
        with pytest.raises(AgentError):
            verify_identity(
                sign(**changes), keys, "oaiapp_test", nonce="nonce-test", subject="subject-test"
            )
    segments = sign().split(".")
    segments[1] = segments[1][:-1] + ("A" if segments[1][-1] != "A" else "B")
    with pytest.raises(AgentError):
        verify_identity(".".join(segments), keys, "oaiapp_test")
    with pytest.raises(AgentError):
        verify_identity(
            jwt.encode({"sub": "x"}, "secret" * 10, algorithm="HS256"), keys, "oaiapp_test"
        )


def test_authorize_uses_own_name_and_reuses_registered_client():
    first = parse_qs(
        urlsplit(
            authorization_url(
                {}, "host", "http://127.0.0.1:5555/auth/callback", "state", "nonce", "verifier"
            )
        ).query
    )
    assert first["agent_name_hint"] == ["Chemistry Agent"]
    assert first["client_id"] == ["dynamic_agent_client"]
    assert first["code_challenge_method"] == ["S256"]
    returning = authorization_url(
        {"client_id": "oaiapp_test", "id_token": "secret-id"},
        "host",
        "callback",
        "state",
        "nonce",
        "verifier",
    )
    assert "agent_name_hint" not in returning and "secret-id" not in returning
    assert parse_qs(urlsplit(returning).query)["client_id"] == ["oaiapp_test"]


def test_tokens_require_explicit_plan_permission():
    with pytest.raises(AgentError):
        token_record(
            {"access_token": "test", "token_type": "Bearer", "expires_in": 3600, "scope": "openid"},
            {},
        )


async def test_loopback_login_preserves_host_and_issued_id_and_checks_nonce(
    settings_factory, signing
):
    sign, keys = signing
    exchanges = []
    query = {}

    def mock(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json=keys)
        if request.url.path == "/token":
            params = parse_qs(request.content.decode())
            exchanges.append(params)
            assert params["client_id"] == ["oaiapp_test"]
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": f"openid {PLAN_SCOPE}",
                    "id_token": sign(nonce=query["nonce"][0]),
                },
            )
        raise AssertionError(request.url)

    plan = ChatGPTPlan(ModelSettingsStore(settings_factory()), transport=httpx.MockTransport(mock))
    attempt = LoginAttempt()
    task = asyncio.create_task(plan.login("alice", attempt))
    try:
        for _ in range(100):
            if attempt.url:
                break
            await asyncio.sleep(0.01)
        assert attempt.url
        query.update(parse_qs(urlsplit(attempt.url).query))
        url = query["redirect_uri"][0]
        host = plan.load("alice")["host_id"]
        async with httpx.AsyncClient(trust_env=False) as client:
            wrong = await client.get(url, params={"state": "wrong", "code": "fake"})
            assert wrong.status_code == 400 and not task.done()
            good = await client.get(
                url,
                params={
                    "state": query["state"][0],
                    "code": "code-test",
                    "client_id": "oaiapp_test",
                },
            )
            assert good.status_code == 200
        await task
        assert attempt.state == "complete" and plan.authenticated("alice")
        assert plan.load("alice")["host_id"] == host
        assert exchanges[0]["redirect_uri"] == [url]
        assert not plan.authenticated("bob")
        public = json.dumps(plan.public("alice"))
        assert "test-access" not in public and "test-refresh" not in public
        assert stat.S_IMODE(plan.path("alice").stat().st_mode) == 0o600
        assert stat.S_IMODE(plan.path("alice").parent.stat().st_mode) == 0o700
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def save_account(plan, user=None, **changes):
    account = {
        "client_id": "oaiapp_test",
        "subject": "subject-test",
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "scopes": [PLAN_SCOPE],
        "expires_at": time.time() + 3600,
    } | changes
    plan.save(
        user, {"active": "oaiapp_test", "selected": True, "accounts": [account], "host_id": "host"}
    )


async def test_refresh_is_serialized_and_rotates_atomically(settings_factory):
    count = 0

    async def mock(request):
        nonlocal count
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                },
            )
        count += 1
        await asyncio.sleep(0.02)
        assert parse_qs(request.content.decode())["refresh_token"] == ["test-refresh"]
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    store = ModelSettingsStore(settings_factory())
    plan = ChatGPTPlan(store, transport=httpx.MockTransport(mock))
    second = ChatGPTPlan(store, transport=httpx.MockTransport(mock))
    save_account(plan, expires_at=0)
    assert (
        await asyncio.gather(plan.access_token(None), second.access_token(None))
        == ["rotated-access"] * 2
    )
    assert count == 1 and plan.active(None)["refresh_token"] == "rotated-refresh"


async def test_logout_retains_registration_and_does_not_fall_back_to_codex(settings_factory):
    def mock(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                },
            )
        assert request.url.path == "/revoke"
        return httpx.Response(200)

    client = ChatGPTClient(ModelSettingsStore(settings_factory()))
    client.plan.transport = httpx.MockTransport(mock)
    save_account(client.plan)
    assert (await client.logout(None))["remote_revoked"]
    assert not await client.authenticated(None)
    assert client.plan.active(None)["client_id"] == "oaiapp_test"
    assert "access_token" not in client.plan.active(None)
    assert client.plan.selected(None)


async def test_response_catalog_and_completed_tool_plan(settings_factory):
    requests = []
    result = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call1",
                "type": "function",
                "function": {"name": "molecular_properties", "arguments": '{"smiles":"O"}'},
            }
        ],
    }

    def mock(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-access"
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "chosen-model", "display_name": "Chosen", "visibility": "list"},
                        {"slug": "hidden", "visibility": "hide"},
                    ]
                },
            )
        data = json.loads(request.content)
        assert data["store"] is False and data["stream"] is True and data["model"] == "chosen-model"
        assert "max_output_tokens" not in data and "previous_response_id" not in data
        payload = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(result)}],
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(payload)}\n\n")

    plan = ChatGPTPlan(ModelSettingsStore(settings_factory()), transport=httpx.MockTransport(mock))
    save_account(plan)
    adapter = PlanResponses(plan)
    assert await adapter.list_models(None) == [{"id": "chosen-model", "name": "Chosen"}]
    answer = await adapter.complete(
        None,
        "chosen-model",
        [{"role": "user", "content": "water"}],
        [{"function": {"name": "molecular_properties"}}],
        allow_tools=True,
    )
    assert answer.message.tool_calls[0].function.name == "molecular_properties"
    assert answer.usage.total_tokens == 30


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "secret-test",
                }
            },
        },
        {"type": "response.incomplete"},
        {"type": "response.output_text.delta", "delta": "partial"},
    ],
)
async def test_partial_and_failed_streams_never_report_success(settings_factory, event):
    def mock(request):
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    plan = ChatGPTPlan(ModelSettingsStore(settings_factory()), transport=httpx.MockTransport(mock))
    save_account(plan)
    with pytest.raises(AgentError) as error:
        await PlanResponses(plan).complete(None, "chosen", [], [], allow_tools=False)
    assert "secret-test" not in str(error.value)


async def test_invalid_refresh_clears_tokens_but_preserves_registration(settings_factory):
    def mock(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                },
            )
        return httpx.Response(400, json={"error": "invalid_grant", "secret": "do-not-return"})

    plan = ChatGPTPlan(ModelSettingsStore(settings_factory()), transport=httpx.MockTransport(mock))
    save_account(plan, expires_at=0)
    with pytest.raises(AgentError) as error:
        await plan.access_token(None)
    assert error.value.code == "chatgpt_reauthorize"
    assert "do-not-return" not in str(error.value)
    assert plan.active(None)["client_id"] == "oaiapp_test"
    assert not plan.authenticated(None)


async def test_failed_new_authorization_does_not_overwrite_existing_account(
    settings_factory, signing
):
    sign, keys = signing

    def mock(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "revocation_endpoint": f"{ISSUER}/revoke",
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json=keys)
        return httpx.Response(
            200,
            json={
                "access_token": "wrong-access",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": PLAN_SCOPE,
                "id_token": sign(aud="oaiapp_new", nonce="invalid-nonce"),
            },
        )

    plan = ChatGPTPlan(ModelSettingsStore(settings_factory()), transport=httpx.MockTransport(mock))
    save_account(plan)
    attempt = LoginAttempt()
    task = asyncio.create_task(plan.login(None, attempt, new_account=True))
    try:
        for _ in range(100):
            if attempt.url:
                break
            await asyncio.sleep(0.01)
        query = parse_qs(urlsplit(attempt.url).query)
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get(
                query["redirect_uri"][0],
                params={"state": query["state"][0], "client_id": "oaiapp_new", "code": "test-code"},
            )
        with pytest.raises(AgentError):
            await task
        assert plan.active(None)["access_token"] == "test-access"
        assert plan.load(None)["last_registration"] == "oaiapp_new"
        assert plan.load(None)["accounts"][1] == {"client_id": "oaiapp_new"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
