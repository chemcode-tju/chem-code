"""Uploaded bytes are private data; chat may use only the uploader's text."""

import json
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.auth import create_user_account


def test_any_file_type_download_and_chat_history(settings_factory):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Received."},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    settings = settings_factory()
    with TestClient(create_app(settings, transport=httpx.MockTransport(respond))) as client:
        text = client.post(
            "/api/files",
            content="CO bond: 1.14 Å".encode(),
            headers={"X-File-Name": quote("../研究.txt")},
        ).json()
        binary = client.post(
            "/api/files",
            content=b"\x00\xff\x89arbitrary",
            headers={"X-File-Name": "sample.unknown"},
        ).json()
        assert text["name"] == "研究.txt"
        assert text["text_available"] is True
        assert binary["text_available"] is False
        download = client.get(f"/api/files/{binary['id']}")
        assert download.content == b"\x00\xff\x89arbitrary"
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")
        result = client.post(
            "/chat", json={"message": "Read files", "file_ids": [text["id"], binary["id"]]}
        )
        assert result.status_code == 200, result.text
        assert "CO bond: 1.14 Å" in str(requests)
        assert "binary files are stored but not parsed" in str(requests)
        session_id = result.json()["session_id"]
        history = client.get(f"/sessions/{session_id}").json()["messages"]
        assert text["id"] in history[0]["content"]
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/files/{text['id']}").content.decode() == "CO bond: 1.14 Å"


def test_file_size_and_request_count_limits(settings_factory, monkeypatch):
    monkeypatch.setattr("chemistry_agent.attachments.MAX_FILE_BYTES", 8)
    with TestClient(create_app(settings_factory())) as client:
        assert client.post("/api/files", content=b"123456789").status_code == 413
        assert client.post("/api/files", content=b"12345678").status_code == 201
        assert (
            client.post(
                "/chat",
                json={"message": "test", "file_ids": ["00000000-0000-0000-0000-000000000000"] * 11},
            ).status_code
            == 422
        )


async def test_file_download_and_model_access_are_account_scoped(settings_factory):
    settings = settings_factory(auth_enabled=True, cookie_secure=False)
    password = "attachment-test-password"
    for username in ("alice", "bob"):
        await create_user_account(settings, username, password)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as alice,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as bob,
    ):
        assert (await bob.post("/api/files", content=b"x")).status_code == 401
        for client, name in [(alice, "alice"), (bob, "bob")]:
            await client.post("/auth/login", json={"username": name, "password": password})
        uploaded = (await alice.post("/api/files", content=b"private result")).json()
        assert (await bob.get(f"/api/files/{uploaded['id']}")).status_code == 404
        assert (
            await bob.post("/chat", json={"message": "read", "file_ids": [uploaded["id"]]})
        ).status_code == 404
        assert (await bob.get("/sessions")).json() == []
        assert (
            await alice.post("/api/files", content=b"x", headers={"Origin": "https://evil.test"})
        ).status_code == 403
