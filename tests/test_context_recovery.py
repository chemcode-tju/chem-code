"""Summary failures must neither erase history nor replay completed tools."""

import copy
import json
from unittest.mock import AsyncMock

import pytest

from chemistry_agent.agent import RunProgress
from chemistry_agent.context import ContextManager
from chemistry_agent.errors import AgentError
from chemistry_agent.provider import AssistantMessage, Completion
from chemistry_agent.runtime.live import LiveAnswer, current_answer
from chemistry_agent.schemas import ContextMemory, TokenUsage
from chemistry_agent.tokens import TokenCounter


def completion(content: str) -> Completion:
    return Completion(
        message=AssistantMessage(role="assistant", content=content),
        usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
    )


@pytest.fixture
def manager(settings_factory):
    settings = settings_factory(
        context_window_tokens=8192,
        deepseek_max_tokens=256,
        context_summary_tokens=256,
        context_recent_turns=1,
        research_memory_enabled=False,
    )
    store = AsyncMock()
    store.get_context_memory.return_value = ContextMemory()
    provider = AsyncMock()
    provider.context_window.return_value = 8192
    provider.complete.return_value = completion("记忆：作业204160已提交；仅核验，不重交。")
    return ContextManager(
        settings, provider, store, TokenCounter(settings.tokenizer_path), "system", []
    )


async def test_chinese_summary_byte_count_is_not_provider_output_tokens(manager):
    content = "表面作业204160已经提交，需要核验输出。" * 10
    assert 256 < manager.counter.text_tokens(content) <= 1024
    manager.provider.complete.return_value = completion(content)
    progress = RunProgress()
    summary = await manager._summarize("", "history", progress)
    assert summary == content
    assert progress.context.summary_memory_budget == 1024
    assert progress.context.summary_calls == 1
    assert manager.provider.complete.call_args.kwargs["max_tokens"] == 256


async def test_oversized_summary_has_one_repair_and_accounts_for_both_calls(manager):
    manager.provider.complete.side_effect = [completion("长" * 2000), completion("作业已提交")]
    progress = RunProgress()
    assert await manager._summarize("original", "new history", progress) == "作业已提交"
    inputs = [
        json.loads(call.args[0][1]["content"]) for call in manager.provider.complete.call_args_list
    ]
    # AsyncMock records mutable messages, so compare transcript and original memory.
    assert all(item["previous_memory"] == "original" for item in inputs)
    assert all(item["new_transcript"] == "new history" for item in inputs)
    assert progress.context.summary_attempts == progress.context.summary_calls == 2
    assert progress.usage.total_tokens == 120
    manager.store.save_context_memory.assert_not_called()


async def test_provider_diagnostics_retry_after_and_preview_isolation(manager):
    preview = LiveAnswer(text="user-visible answer")
    token = current_answer.set(preview)

    async def fail(*args, **kwargs):
        assert current_answer.get() is None
        assert kwargs["thinking"] is False and kwargs["allow_tools"] is False
        raise AgentError("provider_rate_limited", "safe", 503, retry_after=7200)

    manager.provider.complete.side_effect = fail
    progress = RunProgress()
    try:
        with pytest.raises(AgentError) as caught:
            await manager._summarize("", "history", progress)
        assert caught.value.code == "context_summary_failed"
        assert caught.value.cause_code == "provider_rate_limited"
        assert caught.value.retry_after == 7200
        assert progress.context.summary_error_stage == "provider"
        assert progress.context.summary_attempts == 1
        assert current_answer.get() is preview
        assert preview.text == "user-visible answer"
    finally:
        current_answer.reset(token)


async def test_optional_failure_continues_full_history_once_per_run(manager):
    history = [
        {"role": "user", "content": "old " * 1150},
        {"role": "assistant", "content": "known"},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "answer"},
    ]
    current = [{"role": "user", "content": "continue"}]
    messages = history + current
    original = copy.deepcopy(messages)
    manager.provider.complete.side_effect = AgentError("provider_timeout", "timeout", 504)
    progress = RunProgress()
    progress.context.input_budget_tokens = 1  # An old checkpoint must not control sizing.
    manager.settings.context_trigger_ratio = 0.4
    wire = await manager.prepare(messages, len(history), "session", "run", progress)
    assert wire[1:] == messages == original
    assert progress.context.summary_deferred
    assert progress.context.input_tokens_after <= progress.context.input_budget_tokens
    await manager.prepare(messages, len(history), "session", "run", progress)
    assert manager.provider.complete.call_count == 1
    manager.store.save_context_memory.assert_not_called()
    with pytest.raises(AgentError):
        await manager.prepare(messages, len(history), "session", "run", progress, force=True)


async def test_required_later_chunk_failure_preserves_old_memory_and_transcript(manager):
    memory = ContextMemory(summary="previous", covered_messages=2)
    manager.store.get_context_memory.return_value = memory
    manager.provider.complete.side_effect = [
        completion("first chunk"),
        AgentError("provider_timeout", "timeout", 504),
    ]
    messages = [
        {"role": "user", "content": "already covered"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "history " * 2500},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest user intent"},
    ]
    original = copy.deepcopy(messages)
    progress = RunProgress()
    with pytest.raises(AgentError) as caught:
        await manager.prepare(messages, 4, "session", "run", progress)
    assert caught.value.cause_code == "provider_timeout"
    assert progress.context.summary_calls == 1
    assert progress.context.summary_attempts == 2
    manager.store.save_context_memory.assert_not_called()
    assert memory.covered_messages == 2 and memory.summary == "previous"
    assert messages == original


async def test_summary_repair_respects_whole_request_fit(manager):
    manager.provider.complete.side_effect = [completion("x" * 800), completion("small")]
    progress = RunProgress()
    assert (
        await manager._summarize("", "history", progress, fits=lambda text: len(text) < 100)
        == "small"
    )
    assert progress.context.summary_calls == 2


async def test_active_plain_tool_output_does_not_crash_or_mutate(manager):
    current = [
        {"role": "user", "content": "exact active task"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "shell",
                    "type": "function",
                    "function": {"name": "remote_shell", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "shell", "content": "plain output"},
    ]
    original = copy.deepcopy(current)
    wire, _ = manager._wire([], current, ContextMemory())
    assert wire[1:] == current == original


async def test_same_chat_receipt_resumes_after_summary_failure_without_replaying_tool(
    settings_factory, monkeypatch
):
    import asyncio
    from uuid import uuid4

    import httpx

    from chemistry_agent.api import create_app
    from chemistry_agent.context import SUMMARY_SYSTEM_PROMPT

    tool_executions = []
    inference = 0

    def execute_tool(args):
        tool_executions.append(args)
        return {"formula": "C2H6O", "canonical_smiles": "CCO"}

    monkeypatch.setattr("chemistry_agent.tools.molecular_properties", execute_tool)

    def provider(request):
        nonlocal inference
        messages = json.loads(request.content)["messages"]
        if messages[0]["content"] == SUMMARY_SYSTEM_PROMPT:
            return httpx.Response(429, headers={"Retry-After": "60"})
        inference += 1
        if inference == 1:
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "execute-once",
                        "type": "function",
                        "function": {
                            "name": "molecular_properties",
                            "arguments": '{"smiles":"CCO"}',
                        },
                    }
                ],
            }
        else:
            assert messages[-1]["tool_call_id"] == "execute-once"
            message = {"role": "assistant", "content": "recovered"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if inference == 1 else "stop",
                    }
                ]
            },
        )

    app = create_app(settings_factory(), transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        manager = app.state.agent.context_manager
        prepare = manager.prepare
        attempted = False

        async def with_summary(messages, *args, **kwargs):
            nonlocal attempted
            if messages[-1]["role"] == "tool" and not attempted:
                attempted = True
                await manager._summarize("", "older history", args[3])
            return await prepare(messages, *args, **kwargs)

        manager.prepare = with_summary
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            body = {
                "session_id": session,
                "request_id": str(uuid4()),
                "message": "calculate ethanol",
            }
            assert (await client.post("/chat/requests", json=body)).status_code == 202
            await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
            receipts = (await client.get(f"/sessions/{session}/chat-requests")).json()["requests"]
            assert len(receipts) == 1 and receipts[0]["status"] == "failed"
            assert "provider_rate_limited" in receipts[0]["error"]
            assert (await client.post("/chat/requests?retry=true", json=body)).status_code == 202
            await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
            receipts = (await client.get(f"/sessions/{session}/chat-requests")).json()["requests"]
            assert len(receipts) == 1 and receipts[0]["status"] == "completed"
            messages = (await client.get(f"/sessions/{session}")).json()["messages"]
            assert [m["content"] for m in messages if m["role"] == "user"] == [body["message"]]
            assert len(tool_executions) == 1
            assert inference == 2
