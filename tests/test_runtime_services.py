import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from test_agent_api import completion, tool_call

from chemistry_agent.api import create_app
from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.completion_stream import read_chat_stream
from chemistry_agent.runtime.live import LiveAnswer, StructuredPreview, current_answer
from chemistry_agent.runtime.search import SearchInput, web_search_tool
from chemistry_agent.runtime.shell import (
    ShellInput,
    authorized_host,
    remote_shell_tool,
    shell_grant,
)
from chemistry_agent.runtime.turn_memory import fold_tool_rounds
from chemistry_agent.tokens import TokenCounter
from chemistry_agent.tools import ToolContext

CONTEXT = ToolContext("test-session", "test-run")


async def test_search_limits_disclosure_and_returns_safe_deduplicated_sources():
    def serve(request):
        assert str(request.url) == "https://api.mwmbl.org/api/v2/search/?q=VASP+ENCUT"
        assert not request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "javascript:bad", "title": "bad"},
                    *[
                        {
                            "url": "https://vasp.at/wiki/ENCUT",
                            "title": "ENCUT",
                            "content": " cutoff  energy ",
                        }
                    ]
                    * 2,
                ]
            },
        )

    tool = web_search_tool("", httpx.MockTransport(serve))
    result = await tool.handler(SearchInput(query="VASP ENCUT"), CONTEXT)
    assert result["results"] == [
        {"title": "ENCUT", "url": "https://vasp.at/wiki/ENCUT", "snippet": "cutoff energy"}
    ]


async def test_remote_shell_requires_original_user_grant_and_bounds_output(tmp_path, monkeypatch):
    # No network: verify the actual SSH argument/stdin boundary using a local test executable.
    stub = tmp_path / "ssh"
    stub.write_text(
        '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\n'
        '[ "$2" = cluster-b ] && [ "$3" = "sh -s" ] || exit 17\nexec /bin/sh -s\n'
    )
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")
    hosts = ("cluster-b",)
    assert authorized_host("quoted:\n/shell cluster-b", hosts) is None
    with pytest.raises(AgentError):
        authorized_host("/shell -oProxyCommand", hosts)
    tool = remote_shell_tool(hosts)
    with pytest.raises(AgentError):
        await tool.handler(ShellInput(command="pwd"), CONTEXT)
    token = shell_grant.set(authorized_host("/shell cluster-b\nshow", hosts))
    try:
        result = await tool.handler(
            ShellInput(command="yes test | head -c 30000; echo diag >&2; exit 7"), CONTEXT
        )
        assert result["exit_code"] == 7 and result["stderr"].strip() == "diag"
        assert result["truncated"] and len(result["stdout"]) == 16384
    finally:
        shell_grant.reset(token)


async def test_stream_emits_text_before_completion_and_assembles_tool_arguments():
    live = LiveAnswer()
    received, release = asyncio.Event(), asyncio.Event()

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i, delta in enumerate(
                [
                    {"reasoning_content": "secret", "content": "可见"},
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "one",
                                "function": {"name": "search", "arguments": "{"},
                            }
                        ]
                    },
                    {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                ]
            ):
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {"delta": delta, "finish_reason": "tool_calls" if i == 2 else None}
                            ]
                        }
                    )
                    + "\n\n"
                ).encode()
                if i == 0:
                    received.set()
                    await release.wait()
            yield b'data: {"choices":[],"usage":{"total_tokens":8}}\n\ndata: [DONE]\n\n'

    task = asyncio.create_task(read_chat_stream(httpx.Response(200, stream=Chunks()), live))
    await asyncio.wait_for(received.wait(), 2)
    assert live.text == "可见" and not task.done()
    release.set()
    result = await task
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert result["usage"]["total_tokens"] == 8
    with pytest.raises(AgentError, match="中断"):
        await read_chat_stream(httpx.Response(200, text='data: {"choices":[]}\n\n'), live)


def test_json_preview_exposes_only_top_level_unescaped_content():
    live = LiveAnswer()
    token = current_answer.set(live)
    try:
        partial = StructuredPreview()
        partial.feed('{"tool_calls":[{"content":"secret"}],"content":"Line\\n中')
        assert live.text == "Line\n中"
    finally:
        current_answer.reset(token)


def test_folded_turn_keeps_latest_complete_pair_and_original_messages(tmp_path):
    turns = [{"role": "user", "content": "研究目标"}]
    for i in range(4):
        turns += [
            {"role": "assistant", "tool_calls": [tool_call(call_id=str(i))]},
            {
                "role": "tool",
                "tool_call_id": str(i),
                "content": json.dumps({"plan_id": "plan-1", "log": "x" * 1000}),
            },
        ]
    wire, count = fold_tool_rounds(turns, TokenCounter(tmp_path / "missing"), 500)
    assert len(turns) == 9 and count == 3
    assert wire[0] == turns[0] and wire[-2:] == turns[-2:]
    assert "plan-1" in wire[1]["content"]


@pytest.mark.parametrize("interrupt_tool", [False, True])
async def test_restart_after_tool_boundary_never_reexecutes_tool(
    settings_factory, monkeypatch, interrupt_tool
):
    import chemistry_agent.tools as registry

    called = 0
    paused = asyncio.Event()
    original = registry.molecular_properties

    async def effect(args):
        nonlocal called
        called += 1
        if interrupt_tool:
            paused.set()
            await asyncio.Event().wait()
        return original(args)

    monkeypatch.setattr(registry, "molecular_properties", effect)
    settings = settings_factory()

    async def first(request):
        if json.loads(request.content)["messages"][-1]["role"] == "tool":
            paused.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=completion(None, calls=[tool_call()]))

    app = create_app(settings, transport=httpx.MockTransport(first))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        session = (await client.post("/sessions", json={})).json()["id"]
        body = {"session_id": session, "request_id": str(uuid4()), "message": "Use tool"}
        assert (await client.post("/chat/requests", json=body)).status_code == 202
        await asyncio.wait_for(paused.wait(), 3)

    def resume(request):
        result = json.loads(request.content)["messages"][-1]
        assert result["role"] == "tool" and json.loads(result["content"])["formula"] == "C2H6O"
        return httpx.Response(200, json=completion("done"))

    app = create_app(settings, transport=httpx.MockTransport(resume))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 3)
        receipt = (await client.get(f"/sessions/{session}/chat-requests")).json()["requests"][0]
        assert receipt["status"] == ("uncertain" if interrupt_tool else "completed")
        assert (await client.post("/chat/requests?retry=true", json=body)).json()[
            "status"
        ] == receipt["status"]
        assert called == 1
        assert (
            await client.get(f"/sessions/{uuid4()}/chat-requests/{body['request_id']}/stream")
        ).status_code == 404
