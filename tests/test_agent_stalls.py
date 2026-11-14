import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.agent import RunProgress
from chemistry_agent.api import create_app
from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.loop_guard import ObservationWatch
from chemistry_agent.runtime.turn_memory import fold_tool_rounds
from chemistry_agent.schemas import TokenUsage, ToolExecution
from chemistry_agent.store import Store
from chemistry_agent.tokens import TokenCounter


async def test_cancel_after_sqlite_begin_does_not_poison_shared_connection(tmp_path, monkeypatch):
    store = await Store.open(tmp_path / "cancel.sqlite3")
    execute = store.db.execute

    async def cancelled_begin(sql, parameters=()):
        result = await execute(sql, parameters)
        if sql == "BEGIN IMMEDIATE":
            raise asyncio.CancelledError()
        return result

    try:
        monkeypatch.setattr(store.db, "execute", cancelled_begin)
        with pytest.raises(asyncio.CancelledError):
            async with store.transaction():
                pytest.fail("Cancelled BEGIN must not enter the transaction body")
        monkeypatch.setattr(store.db, "execute", execute)
        assert not store.db.in_transaction
        assert (await store.create_session("connection recovered"))["id"]
    finally:
        await store.close()


async def test_restart_reconciles_orphaned_delivery_even_when_plan_is_paused(tmp_path):
    path = tmp_path / "orphan.sqlite3"
    store = await Store.open(path)
    session = (await store.create_session("paused"))["id"]
    run, _ = await store.begin_run(session)
    async with store.transaction():
        await store.db.execute(
            "INSERT INTO agent_events(id,session_id,plan_id,payload,status,phase,"
            "created_at,run_id) "
            "VALUES ('event',?,'paused-plan','{}','running','tools',0,?)",
            (session, run),
        )
    await store.close()
    store = await Store.open(path)
    try:
        event = await store._one("SELECT * FROM agent_events WHERE id='event'")
        assert event["status"] == "uncertain"  # inspect the unknown command before any replay
        assert (await store.get_run(run))["status"] == "interrupted"
    finally:
        await store.close()


def test_repeat_guard_allows_changing_results_and_has_no_parallel_job_limit():
    watch = ObservationWatch()
    for index in range(100):
        watch.observe(
            [
                ToolExecution(
                    call_id=str(index),
                    name="inspect",
                    arguments={},
                    success=True,
                    result={"step": index},
                )
            ]
        )
        watch.check()
    same = ToolExecution(
        call_id="new-id", name="inspect", arguments={}, success=True, result={"step": 99}
    )
    for _ in range(8):
        watch.observe([same])
    with pytest.raises(AgentError, match="重复"):
        watch.check()


def test_real_agent_stops_repeated_observations_before_another_model_call(settings_factory):
    count = 0

    def provider(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "再次检查相同分子。",
                            "tool_calls": [
                                {
                                    "id": f"call-{count}",
                                    "type": "function",
                                    "function": {
                                        "name": "molecular_properties",
                                        "arguments": '{"smiles":"CCO"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    with TestClient(
        create_app(settings_factory(max_tool_rounds=20), transport=httpx.MockTransport(provider))
    ) as client:
        result = client.post("/chat", json={"message": "检查乙醇"})
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "agent_no_progress"
        assert count == 9


def test_resume_usage_counts_only_new_provider_calls():
    progress = RunProgress(
        usage=TokenUsage(prompt_tokens=1000, completion_tokens=10, total_tokens=1010)
    )
    progress.usage_baseline = progress.usage.model_copy()
    progress.usage = progress.usage.plus(
        TokenUsage(prompt_tokens=20, completion_tokens=2, total_tokens=22)
    )
    assert progress.current_usage.total_tokens == 22
    assert progress.usage.total_tokens == 1032  # durable event checkpoint remains cumulative


def test_fold_keeps_action_receipt_and_three_recent_rounds(tmp_path):
    messages = [{"role": "system", "content": "completion event"}]
    for index in range(25):
        name = "calculation_step_report" if index == 0 else "calculation_plan_get"
        result = {"node_id": "hollow", "status": "completed", "accepted_jobs": ["204170"]}
        if index:
            result = {"id": "same-plan", "revision": 7}
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": str(index), "function": {"name": name}}],
                },
                {"role": "tool", "tool_call_id": str(index), "content": json.dumps(result)},
            ]
        )
    output, count = fold_tool_rounds(messages, TokenCounter(tmp_path / "missing"), 1024)
    assert count == 22
    assert "calculation_step_report" in output[1]["content"]
    assert "204170" in output[1]["content"]
    assert output[-6:] == messages[-6:]
