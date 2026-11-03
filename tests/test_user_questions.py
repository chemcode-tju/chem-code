import asyncio
import json
from uuid import uuid4

import httpx

from chemistry_agent.api import create_app

ASK = {
    "id": "sites",
    "question": "CO 要比较哪些位点？",
    "multi_select": True,
    "options": [{"label": "top"}, {"label": "bridge"}],
}


def completion(message: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", **message},
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                }
            ]
        },
    )


def call(name: str, args: dict) -> dict:
    return {
        "id": str(uuid4()),
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


async def test_persistent_question_answers_once_after_restart(settings_factory):
    prompts = []

    def provider(request):
        prompts.append(json.loads(request.content))
        if len(prompts) == 1:
            return completion({"tool_calls": [call("ask_user_question", {"questions": [ASK]})]})
        return completion({"content": "已按选择更新草稿。"})

    settings = settings_factory()
    app = create_app(settings, transport=httpx.MockTransport(provider))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        original = (
            await client.post("/chat", json={"message": "帮我设计 CO 位点比较方案。"})
        ).json()
        record = original["tool_calls"][0]["result"]
        assert record["status"] == "awaiting_user" and len(prompts) == 1
        sid, key = original["session_id"], record["question_id"]

    app = create_app(settings, transport=httpx.MockTransport(provider))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        messages = (await client.get(f"/sessions/{sid}")).json()["messages"]
        assert key in next(m["content"] for m in messages if m["role"] == "tool")
        url = f"/sessions/{sid}/questions/{key}/answer"
        reply = {
            "answers": [{"id": "sites", "selected": ["top", "bridge"], "custom": "只做第一轮"}]
        }
        assert (await client.post(url.replace(sid, str(uuid4())), json=reply)).status_code == 404
        assert (
            await client.post(url, json={"answers": [{"id": "sites", "selected": ["fcc"]}]})
        ).status_code == 422
        first, duplicate = await asyncio.gather(
            client.post(url, json=reply), client.post(url, json=reply)
        )
        assert first.status_code == duplicate.status_code == 202
        await asyncio.gather(*app.state.chat_queue.tasks)
        assert len(prompts) == 2
        assert "top；bridge；只做第一轮" in prompts[-1]["messages"][-1]["content"]
        assert (await client.post(url, json=reply)).status_code == 202
        changed = {"answers": [{"id": "sites", "selected": ["bridge"]}]}
        assert (await client.post(url, json=changed)).status_code == 409
        assert len(prompts) == 2


async def test_batched_question_never_executes_sibling_and_old_card_is_closed(settings_factory):
    calls = 0

    def provider(request):
        nonlocal calls
        calls += 1
        question = call("ask_user_question", {"questions": [ASK]})
        if calls == 1:
            return completion(
                {"tool_calls": [question, call("molecular_properties", {"smiles": "C"})]}
            )
        if calls == 2:
            previous = json.loads(request.content)["messages"][-2:]
            assert all(json.loads(m["content"])["error"] == "ask_separately" for m in previous)
            return completion({"tool_calls": [question]})
        return completion({"content": "好的。"})

    app = create_app(settings_factory(), transport=httpx.MockTransport(provider))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = (await client.post("/chat", json={"message": "选位点"})).json()
        question = response["tool_calls"][-1]["result"]
        sid = response["session_id"]
        await client.post("/chat", json={"session_id": sid, "message": "先跳过这一步。"})
        assert (
            await client.post(
                f"/sessions/{sid}/questions/{question['question_id']}/answer",
                json={"answers": [{"id": "sites", "custom": "另选"}]},
            )
        ).status_code == 409
