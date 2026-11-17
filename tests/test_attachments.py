"""Uploaded bytes are private data; chat may use only the uploader's text."""

import json
import sqlite3
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


def test_large_original_many_files_and_legacy_blob(settings_factory):
    from chemistry_agent.schemas import ChatRequest

    settings = settings_factory()
    with TestClient(create_app(settings)) as client:
        content = b"x" * (25 * 1024 * 1024 + 1)
        large = client.post("/api/files", content=content).json()
        assert large["size"] == len(content) and large["text_available"]
        assert (
            client.get(f"/api/files/{large['id']}", headers={"Range": "bytes=0-15"}).content
            == content[:16]
        )
        ids = [client.post("/api/files", content=b"x").json()["id"] for _ in range(11)]
        assert len(ChatRequest(message="read all", file_ids=ids).file_ids) == 11
        legacy = "10000000-0000-4000-8000-000000000001"
        with sqlite3.connect(settings.database_path) as db:
            db.execute(
                "INSERT INTO attachments(id,name,size,content) VALUES(?,?,?,?)",
                (legacy, "legacy.xyz", 3, b"old"),
            )
        assert client.get(f"/api/files/{legacy}").content == b"old"


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


def test_uploaded_geometry_is_bound_to_its_chat_and_reusable(settings_factory, monkeypatch):
    from chemistry_agent import structure_inputs

    async def validate(action: str, payload: dict) -> dict:
        assert action == "validate_structure_file"
        return structure_inputs.validate_structure_file(payload)

    def reply(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "Saved."}}
                ]
            },
        )

    settings = settings_factory(automation_enabled=True, automation_monitor_enabled=False)
    with TestClient(
        create_app(settings, transport=httpx.MockTransport(reply)),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 5000),
    ) as client:
        monkeypatch.setattr(client.app.state.calculation_chat.service, "call", validate)
        session, other = [client.post("/sessions", json={}).json()["id"] for _ in range(2)]
        poscar = b"Cu\n1\n3 0 0\n0 3 0\n0 0 3\nCu\n1\nSelective dynamics\nDirect\n0 0 0 T F F\n"
        files = [
            client.post("/api/files", content=data, headers={"X-File-Name": name}).json()
            for data, name in [(poscar, "POSCAR"), (b"1\nframe\nH 0 0 0\n" * 2, "frames.xyz")]
        ]
        response = client.post(
            "/chat",
            json={
                "session_id": session,
                "message": "inspect",
                "file_ids": [file["id"] for file in files],
            },
        )
        assert response.status_code == 200, response.text
        url = f"/api/automation/chat/{session}/structures"
        body = {"file_id": files[0]["id"]}
        first, repeat = [client.post(url, json=body) for _ in range(2)]
        assert first.status_code == 200, first.text
        assert first.json()["id"] == repeat.json()["id"] != files[0]["id"]
        assert first.json()["positions_dof"] == [[True, False, False]]
        assert client.post(f"/api/automation/chat/{other}/structures", json=body).status_code == 404
        with sqlite3.connect(settings.database_path) as db:
            db.execute("DELETE FROM session_files WHERE session_id=?", (session,))
            db.execute(
                "INSERT INTO chat_requests(id,session_id,created_at,status,body) "
                "VALUES ('old-receipt',?,'2026-10-31','completed',?)",
                (session, json.dumps({"file_ids": [file["id"] for file in files]})),
            )
        assert client.post(url, json=body).json()["id"] == first.json()["id"]
        assert client.post(f"/api/automation/chat/{other}/structures", json=body).status_code == 404
        assert client.post(url, json={"file_id": files[1]["id"]}).status_code == 422
        assert client.get("/api/files/" + files[1]["id"]).content.count(b"frame") == 2
