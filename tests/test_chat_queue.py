import asyncio
from uuid import uuid4

import httpx

from chemistry_agent.api import create_app


def completion(text: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
    }


async def test_navigation_reconnect_queue_and_duplicate_receipts(settings_factory):
    started, release = asyncio.Event(), asyncio.Event()
    prompts = []

    async def provider(request):
        import json

        data = json.loads(request.content)
        prompts.append(data["messages"])
        if len(prompts) == 1:
            started.set()
            await release.wait()
        return httpx.Response(200, json=completion(f"回复 {len(prompts)}"))

    app = create_app(settings_factory(), transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as tab:
            session = (await tab.post("/sessions", json={})).json()["id"]
            first = {"session_id": session, "request_id": str(uuid4()), "message": "第一条"}
            assert (await tab.post("/chat/requests", json=first)).status_code == 202
            await asyncio.wait_for(started.wait(), 3)
        # Leaving the chat page closes the client. The model is still processing.
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as tab:
            path = f"/sessions/{session}/chat-requests"
            saved = (await tab.get(path)).json()
            assert saved["requests"][0]["body"]["message"] == "第一条"
            assert saved["requests"][0]["status"] == "running"
            assert (await tab.post("/chat/requests", json=first)).status_code == 202
            second = {**first, "request_id": str(uuid4()), "message": "第二条"}
            assert (await tab.post("/chat/requests", json=second)).status_code == 202
            saved = (await tab.get(path)).json()
            assert [item["status"] for item in saved["requests"]] == ["running", "queued"]
            release.set()
            await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
            history = (await tab.get(f"/sessions/{session}")).json()["messages"]
            assert [m["content"] for m in history if m["role"] == "user"] == ["第一条", "第二条"]
            assert len(prompts) == 2
            assert any(m.get("content") == "回复 1" for m in prompts[1])
            # A retry after a lost HTTP receipt must not run the same message twice.
            assert (await tab.post("/chat/requests", json=first)).json()["status"] == "completed"
            assert len(prompts) == 2


async def test_failed_and_interrupted_requests_preserve_text_and_can_retry(settings_factory):
    settings = settings_factory()
    started = asyncio.Event()

    async def wait(request):
        started.set()
        await asyncio.Event().wait()

    app = create_app(settings, transport=httpx.MockTransport(wait))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            body = {"session_id": session, "request_id": str(uuid4()), "message": "原文必须保留"}
            assert (await client.post("/chat/requests", json=body)).status_code == 202
            await asyncio.wait_for(started.wait(), 3)
    app = create_app(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion("恢复完成"))
        ),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = f"/sessions/{session}/chat-requests"
            item = (await client.get(path)).json()["requests"][0]
            assert item["status"] == "failed"
            assert item["body"]["message"] == body["message"]
            assert (await client.post("/chat/requests", json=body)).json()["status"] == "failed"
            assert (await client.post("/chat/requests?retry=true", json=body)).status_code == 202
            await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
            assert (await client.get(path)).json()["requests"][0]["status"] == "completed"


async def test_receipt_conflict_and_unknown_session(settings_factory):
    app = create_app(
        settings_factory(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion("完成"))),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            body = {"session_id": session, "request_id": str(uuid4()), "message": "原文"}
            assert (await client.post("/chat/requests", json=body)).status_code == 202
            response = await client.post("/chat/requests", json={**body, "message": "不同内容"})
            assert response.status_code == 409
            assert (await client.get(f"/sessions/{uuid4()}/chat-requests")).status_code == 404


async def test_subscription_turns_wait_before_starting_run(settings_factory, monkeypatch):
    """Five sessions must not burn their run timeout waiting for one account's inference."""
    from chemistry_agent.chatgpt import ChatGPTClient
    from chemistry_agent.model_settings import ModelSettings
    from chemistry_agent.provider import AssistantMessage, Completion
    from chemistry_agent.schemas import TokenUsage

    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def authenticated(self, user_id):
        return True

    async def respond(self, user_id, config, messages, tools, **kwargs):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            started.set()
            await release.wait()
        return Completion(
            message=AssistantMessage(role="assistant", content="done", tool_calls=[]),
            usage=TokenUsage(),
        )

    monkeypatch.setattr(ChatGPTClient, "authenticated", authenticated)
    monkeypatch.setattr(ChatGPTClient, "complete", respond)
    app = create_app(settings_factory())
    async with app.router.lifespan_context(app):
        app.state.provider.store.save(None, ModelSettings(provider="chatgpt", model="test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = (await client.post("/sessions", json={})).json()["id"]
            second = (await client.post("/sessions", json={})).json()["id"]
            for sid in (first, second):
                await client.post(
                    "/chat/requests",
                    json={
                        "session_id": sid,
                        "request_id": str(uuid4()),
                        "message": sid,
                    },
                )
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.sleep(0.02)
            pending = (await client.get(f"/sessions/{second}/chat-requests")).json()
            assert pending["active_run_id"] is None
            assert pending["requests"][0]["status"] == "queued"
            assert calls == [first]
            release.set()
            await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
            assert calls == [first, second]
            done = (await client.get(f"/sessions/{second}/chat-requests")).json()
            assert done["requests"][0]["status"] == "completed"
