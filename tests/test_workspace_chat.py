import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from chemistry_agent.api import create_app


async def test_late_guidance_and_side_question_have_independent_history(settings_factory):
    started, proceed = asyncio.Event(), asyncio.Event()
    calls = []

    async def provider(request):
        prompts = json.loads(request.content)["messages"]
        calls.append(prompts)
        if len(calls) == 1:
            started.set()
            await proceed.wait()
        text = prompts[-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                ]
            },
        )

    app = create_app(settings_factory(), transport=httpx.MockTransport(provider))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        session = (await client.post("/sessions", json={})).json()["id"]
        target = str(uuid4())
        await client.post(
            "/chat/requests", json={"session_id": session, "request_id": target, "message": "轨迹"}
        )
        await asyncio.wait_for(started.wait(), 3)
        path = f"/sessions/{session}/chat-requests"
        run = (await client.get(path)).json()["active_run_id"]
        base = f"{path}/{target}/guidance"
        note, extra = f"{base}/{uuid4()}", f"{base}/{uuid4()}"

        async def put(url, text, expected=200):
            response = await client.put(url, json={"run_id": run, "message": text})
            assert response.status_code == expected

        await put(note, "10 ps")
        await put(note, "5 ps")
        await put(extra, "取消这条")
        assert (await client.delete(extra)).json()["status"] == "withdrawn"
        elsewhere = (await client.post("/sessions", json={})).json()["id"]
        await put(note.replace(session, elsewhere), "5 ps", 404)
        side = f"/sessions/{session}/side-chat"
        question = {"id": str(uuid4()), "question": "这个单位是什么？"}
        assert (await client.post(side, json=question)).json()["answer"] == question["question"]
        assert "轨迹" in calls[-1][1]["content"]
        await client.post(side, json=question)
        assert len(calls) == 2
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
        messages = (await client.get(f"/sessions/{session}")).json()["messages"]
        assert messages[0] == {"role": "user", "content": "轨迹", "guidance": ["5 ps"]}
        assert messages[-1]["content"] == "5 ps"
        assert all(question["question"] not in str(row) for row in calls[-1])
        assert [
            row["status"] for row in (await client.get(path)).json()["requests"][0]["guidance"]
        ] == ["included", "withdrawn"]
        assert len((await client.get(side)).json()["items"]) == 1
        assert (await client.get(f"/sessions/{elsewhere}/side-chat")).json()["items"] == []
        await put(f"{base}/{uuid4()}", "太迟", 409)
        assert (await client.get("/api/chat/attention")).json()["statuses"][session] == "completed"


def test_web_terminal_uses_bound_job_directory_and_checks_browser_origin(
    settings_factory, monkeypatch
):
    asyncssh = pytest.importorskip("asyncssh")
    app = create_app(settings_factory(ssh_hosts="qa-host"))
    root = app.state.settings.platform_path
    host_key, user_key = (
        asyncssh.generate_private_key("ssh-ed25519"),
        asyncssh.generate_private_key("ssh-ed25519"),
    )

    class TestServer(asyncssh.SSHServer):
        def public_key_auth_supported(self):
            return True

        def validate_public_key(self, username, key):
            return key == user_key.convert_to_public()

    observed = []

    async def shell(process):
        observed.append((process.command, process.get_terminal_type()))
        process.stdout.write(b"connected\r\n")
        try:
            while data := await process.stdin.read(1024):
                process.stdout.write(data)
        except asyncssh.TerminalSizeChanged as change:
            observed.append((change.width, change.height))
            process.stdout.write(b"resized\r\n")
            await process.stdin.read()
        finally:
            process.exit(0)

    async def start():
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=TestServer,
            server_host_keys=[host_key],
            process_factory=shell,
            encoding=None,
            line_editor=False,
        )

    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        server = client.portal.call(start)

        @asynccontextmanager
        async def connect(hosting, host):
            assert host == "qa-host"
            async with asyncssh.connect(
                "127.0.0.1",
                port=server.get_port(),
                username="qa",
                client_keys=[user_key],
                agent_path=None,
                known_hosts=([host_key.convert_to_public()], [], []),
            ) as ssh:
                yield ssh

        monkeypatch.setattr("chemistry_agent.hosting.terminal.connection", connect)
        sid = client.post("/sessions", json={}).json()["id"]
        job = str(uuid4())
        directory = root / "chat" / sid / "slurm_jobs"
        directory.mkdir(parents=True)
        (directory / f"{job}.json").write_text(
            json.dumps(
                {
                    "id": job,
                    "host": "qa-host",
                    "scheduler_job_id": "123",
                    "remote_directory": "/test/space here",
                }
            )
        )
        location = client.get(f"/api/hosting/sessions/{sid}/location").json()
        assert location["host"] == "local"
        assert location["targets"][0]["directory"] == "/test/space here"
        url = f"/api/hosting/sessions/{sid}/terminal?host=qa-host&directory=/test/space%20here"
        try:
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(url, headers={"origin": "https://other.test"}),
            ):
                pass
            with client.websocket_connect(
                "ws://localhost" + url, headers={"origin": "http://localhost"}
            ) as ws:
                assert ws.receive_json() == {"status": "connected"}
                assert ws.receive_bytes() == b"connected\r\n"
                ws.send_json({"type": "input", "data": "abc\x03"})
                assert ws.receive_bytes() == b"abc\x03"
                ws.send_json({"type": "resize", "cols": 120, "rows": 40})
                assert ws.receive_bytes() == b"resized\r\n"
            assert observed == [
                ("cd -- '/test/space here' && exec \"${SHELL:-/bin/sh}\" -l", "xterm-256color"),
                (120, 40),
            ]
        finally:
            client.portal.call(server.close)
            client.portal.call(server.wait_closed)


async def test_queue_edits_and_withdrawals_are_checked_before_dispatch(settings_factory):
    started, proceed = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        text = json.loads(request.content)["messages"][-1]["content"]
        calls.append(text)
        if len(calls) == 1:
            started.set()
            await proceed.wait()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ]
            },
        )

    app = create_app(settings_factory(), transport=httpx.MockTransport(respond))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        sid = (await client.post("/sessions", json={})).json()["id"]
        ids = [str(uuid4()) for _ in range(3)]
        path = f"/sessions/{sid}/chat-requests"
        for index, key in enumerate(ids):
            await client.post(
                "/chat/requests", json={"session_id": sid, "message": str(index), "request_id": key}
            )
        await asyncio.wait_for(started.wait(), 3)
        assert (
            await client.patch(f"{path}/{ids[0]}", json={"previous": "0", "message": "late"})
        ).status_code == 409
        assert (
            await client.patch(f"{path}/{ids[1]}", json={"previous": "1", "message": "edited"})
        ).status_code == 200
        assert (
            await client.patch(f"{path}/{ids[1]}", json={"previous": "1", "message": "stale"})
        ).status_code == 409
        assert (await client.patch(f"{path}/{ids[2]}", json={"previous": "2"})).status_code == 200
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
        assert calls == ["0", "edited"]
        assert [r["status"] for r in (await client.get(path)).json()["requests"]] == [
            "completed",
            "completed",
            "withdrawn",
        ]
