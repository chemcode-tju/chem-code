"""Crash/retry boundaries and real agent continuation without cluster/model charges."""

import json
import time
from uuid import uuid4

import httpx
import pytest
from test_slurm_jobs import LocalSlurm, job, tick
from test_slurm_jobs import cluster as cluster_fixture

from chemistry_agent.automation.job_events import record_events, wake
from chemistry_agent.automation.plan_authority import auto_queue
from chemistry_agent.automation.plan_models import PlanDraft
from chemistry_agent.automation.wait_ledger import EventCheckpoint, WaitLedger
from chemistry_agent.automation.wait_models import WaitControl, WaitRegister
from chemistry_agent.automation.wait_tools import tools
from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import TokenUsage
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolContext

cluster = cluster_fixture


@pytest.fixture
async def saved(tmp_path):
    store = await Store.open(tmp_path / "waits.sqlite3")
    session = (await store.create_session("waits"))["id"]
    plan = {
        "id": str(uuid4()),
        "goal": "arbitrary study",
        "status": "running",
        "nodes": [{"id": k, "kind": "job"} for k in ("a", "b")],
        "runtime": {k: {"status": "pending", "generic_job_id": str(uuid4())} for k in ("a", "b")},
    }
    yield store, session, plan
    await store.close()


def registration(plan: dict, **kwargs) -> WaitRegister:
    return WaitRegister(
        plan_id=plan["id"],
        key="group",
        instruction="Read outputs and choose the next step",
        condition={
            "op": "all",
            "conditions": [
                {"op": "state", "node": k, "states": ["completed", "failed"]} for k in ("a", "b")
            ],
        },
        **kwargs,
    )


async def test_match_persisted_once_and_group_replaces_default(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    args = registration(plan)
    first = await ledger.register(session, plan, args)
    assert await ledger.register(session, plan, args) == first
    for key in ("a", "b"):
        plan["runtime"][key]["status"] = "completed"
        record_events(plan)
        await ledger.observe(session, plan)
        event = await ledger.pending(session, plan)
        assert (event is None) == (key == "a")
    # Match remains true in the ledger even when the transient condition disappears.
    plan["runtime"]["b"]["status"] = "running"
    await ledger.observe(session, plan)
    assert (await ledger.pending(session, plan))["id"] == first["wait_id"]
    run, _ = await store.begin_run(session, event_id=event["id"])
    await store.complete_run(session, run, [], "done", [], TokenUsage())
    await ledger.observe(session, plan)
    assert await ledger.pending(session, plan) is None
    assert (await ledger.rows(session, plan["id"]))[0]["delivery_status"] == "completed"


async def test_busy_session_does_not_consume_event(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    await ledger.register(session, plan, registration(plan))
    for state in plan["runtime"].values():
        state["status"] = "completed"
    await ledger.observe(session, plan)
    event = await ledger.pending(session, plan)
    user_run, _ = await store.begin_run(session)
    with pytest.raises(AgentError, match="上一条"):
        await store.begin_run(session, event_id=event["id"])
    assert (await ledger.pending(session, plan))["id"] == event["id"]
    await store.complete_run(session, user_run, [], "user", [], TokenUsage())
    run, _ = await store.begin_run(session, event_id=event["id"])
    with pytest.raises(AgentError):
        await store.begin_run(session, event_id=event["id"])
    await store.complete_run(session, run, [], "event", [], TokenUsage())


@pytest.mark.parametrize("phase", ["model", "tools", "completed"])
async def test_process_restart_resumes_only_known_boundaries(tmp_path, phase):
    path = tmp_path / "restart.sqlite3"
    store = await Store.open(path)
    session = (await store.create_session("restart"))["id"]
    plan = {
        "id": str(uuid4()),
        "goal": "test",
        "status": "running",
        "runtime": {"a": {"status": "completed", "generic_job_id": "receipt-a"}},
        "nodes": [{"id": "a", "kind": "job"}],
    }
    record_events(plan)
    ledger = WaitLedger(store)
    await ledger.observe(session, plan)
    event = await ledger.pending(session, plan)
    run, _ = await store.begin_run(session, event_id=event["id"])
    checkpoint = EventCheckpoint(store, event["id"])
    await checkpoint.save({"tail": [{"role": "tool", "content": "already submitted"}]})
    if phase == "tools":
        await checkpoint.mark_tools()
    if phase == "completed":
        await store.complete_run(session, run, [], "finished", [], TokenUsage())
    await store.close()  # Simulate process exit without completing the running event.
    store = await Store.open(path)
    try:
        ledger = WaitLedger(store)
        recovered = await ledger.pending(session, plan)
        assert (recovered is not None) == (phase == "model")
        row = await store._one("SELECT * FROM agent_events WHERE id=?", (event["id"],))
        assert (
            row["status"]
            == {"model": "pending", "tools": "uncertain", "completed": "completed"}[phase]
        )
        assert (await EventCheckpoint(store, event["id"]).load())["tail"][0][
            "content"
        ] == "already submitted"
    finally:
        await store.close()


async def test_late_grouping_suppresses_already_queued_defaults(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    plan["runtime"]["a"]["status"] = "completed"
    record_events(plan)
    await ledger.observe(session, plan)
    assert await ledger.pending(session, plan)
    await ledger.register(session, plan, registration(plan))
    await ledger.observe(session, plan)
    assert await ledger.pending(session, plan) is None


async def test_default_completion_burst_is_batched(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    for state in plan["runtime"].values():
        state["status"] = "completed"
    record_events(plan)
    await ledger.observe(session, plan)
    event = await ledger.pending(session, plan)
    assert len(json.loads(event["payload"])["events"]) == 2
    run, _ = await store.begin_run(session, event_id=event["id"])
    await store.complete_run(session, run, [], "both", [], TokenUsage())
    await ledger.observe(session, plan)
    assert await ledger.pending(session, plan) is None
    rows = await (await store.db.execute("SELECT status FROM agent_events")).fetchall()
    assert {r["status"] for r in rows} == {"completed"}


async def test_expiry_and_old_attempt_do_not_wake(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    await ledger.register(session, plan, registration(plan, expires_at=time.time() - 1))
    for state in plan["runtime"].values():
        state["status"] = "completed"
    record_events(plan)
    await ledger.observe(session, plan)
    assert await ledger.pending(session, plan) is None
    assert (await ledger.rows(session, plan["id"]))[0]["status"] == "expired"
    # Separate plan: an old attempt's result must not operate on its replacement.
    plan["id"] = str(uuid4())
    await ledger.observe(session, plan)
    plan["runtime"]["a"]["generic_job_id"] = "new-attempt"
    plan["runtime"]["b"]["generic_job_id"] = "new-attempt-b"
    assert await ledger.pending(session, plan) is None


async def test_file_wait_observes_running_job_without_requiring_retrieval(cluster):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "file-wait.sqlite3")
    session = (await store.create_session("file"))["id"]
    context = ToolContext(session, context.run_id)
    plans.store = store
    try:
        draft = await plans.draft(PlanDraft(title="file", goal="file", nodes=[job(tmp)]), context)
        await plans.confirm(session, draft["id"], draft["revision"])
        plan = await tick(plans, context, draft["id"])
        assert plan["runtime"]["compute"]["status"] == "running"
        args = WaitRegister(
            plan_id=plan["id"],
            key="marker",
            instruction="Read marker",
            condition={"op": "file", "node": "compute", "pattern": "result.dat"},
        )
        await tools(plans)[0].handler(args, context)
        ledger = WaitLedger(store)
        await ledger.probe_files(plans, session, plan)
        await ledger.observe(session, plan)
        event = await ledger.pending(session, plan)
        assert json.loads(event["payload"])["file_observations"]["compute"]["files"] == [
            "result.dat"
        ]

        # No second remote probe during the registered interval.
        async def forbidden(*args, **kwargs):
            raise AssertionError("polled twice")

        plans.slurm.remote = forbidden
        await ledger.probe_files(plans, session, plan)
        await tools(plans)[1].handler(
            WaitControl(plan_id=plan["id"], action="cancel", wait_id=event["id"]), context
        )
        assert await ledger.pending(session, plan) is None
    finally:
        await store.close()


@pytest.mark.parametrize("failure_boundary", ["inference", "summary"])
async def test_real_agent_resumes_after_429_without_repeating_extension(cluster, failure_boundary):
    from chemistry_agent.api import create_app

    original, _, tmp = cluster
    phase = 0
    plan_id = None

    def completion(request):
        nonlocal phase
        phase += 1
        data = json.loads(request.content)
        if phase == 1:
            content = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "extend-once",
                        "type": "function",
                        "function": {
                            "name": "calculation_plan_extend",
                            "arguments": json.dumps(
                                {
                                    "plan_id": plan_id,
                                    "reason": "continue",
                                    "nodes": [job(tmp, "next")],
                                }
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        elif phase == 2:
            return httpx.Response(429)
        else:
            # The persisted extension result is supplied to inference; the tool isn't rerun.
            last = data["messages"][-1]
            assert last["role"] == "tool" and last["tool_call_id"] == "extend-once"
            assert json.loads(last["content"])["added_nodes"] == ["next"]
            content, finish = {"role": "assistant", "content": "已恢复，下一步已经登记。"}, "stop"
        return httpx.Response(
            200, json={"choices": [{"message": content, "finish_reason": finish}]}
        )

    app = create_app(original.chat.service.settings, transport=httpx.MockTransport(completion))
    async with app.router.lifespan_context(app):
        plans = app.state.calculation_chat.plans
        await plans.close()
        plans.slurm = LocalSlurm(plans.chat)
        session = (await app.state.store.create_session("recovery"))["id"]
        context = ToolContext(session, str(uuid4()))
        token = auto_queue.set("low")
        try:
            plan = await plans.draft(
                PlanDraft(title="recover", goal="continue", nodes=[job(tmp)]), context
            )
        finally:
            auto_queue.reset(token)
        plan_id = plan["id"]
        agent, plans.agent = plans.agent, None
        await tick(plans, context, plan_id)
        await tick(plans, context, plan_id)
        plans.agent = agent
        if failure_boundary == "summary":
            manager = agent.context_manager
            prepare = manager.prepare
            attempted = False

            async def prepare_with_summary(messages, *args, **kwargs):
                nonlocal attempted
                if messages[-1]["role"] == "tool" and not attempted:
                    attempted = True
                    await manager._summarize("", "completed tool history", args[3])
                return await prepare(messages, *args, **kwargs)

            manager.prepare = prepare_with_summary
        with pytest.raises(AgentError) as captured:
            await wake(plans, session, plan_id)
        assert captured.value.code == (
            "context_summary_failed" if failure_boundary == "summary" else "provider_rate_limited"
        )
        await wake(plans, session, plan_id)  # No inference before backoff expires.
        assert phase == 2
        async with plans.store.transaction():
            await plans.store.db.execute(
                "UPDATE agent_events SET available_at=0 WHERE status='retry_wait'"
            )
        await wake(plans, session, plan_id)
        assert phase == 3
        current = plans.chat.load(session, "plans", plan_id)
        assert [n["id"] for n in current["nodes"]].count("next") == 1
        assert current["revision"] == 2
        plans.agent = None
        await tick(plans, context, plan_id)
        await tick(plans, context, plan_id)
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
        assert "后台任务结束事件" not in json.dumps(
            await plans.store.get_session(session), ensure_ascii=False
        )


async def test_any_failure_freezes_only_matching_branch(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    args = registration(plan).model_copy(
        update={
            "condition": WaitRegister(
                plan_id=plan["id"],
                key="any",
                instruction="inspect failure",
                condition={
                    "op": "any",
                    "conditions": [
                        {"op": "state", "node": k, "states": ["failed"]} for k in ("a", "b")
                    ],
                },
            ).condition
        }
    )
    await ledger.register(session, plan, args)
    plan["runtime"]["a"]["status"] = "failed"
    await ledger.observe(session, plan)
    plan["runtime"]["b"]["generic_job_id"] = "newly-submitted"
    event = await ledger.pending(session, plan)
    assert [e["node_id"] for e in json.loads(event["payload"])["events"]] == ["a"]


async def test_retry_after_and_unknown_tool_intent_are_persisted(saved):
    from chemistry_agent.provider import DeepSeekClient

    store, session, plan = saved
    ledger = WaitLedger(store)
    plan["runtime"]["a"]["status"] = "completed"
    record_events(plan)
    await ledger.observe(session, plan)
    event = await ledger.pending(session, plan)
    with pytest.raises(AgentError) as captured:
        DeepSeekClient.check_status(httpx.Response(429, headers={"Retry-After": "7200"}))
    await ledger.failure(event["id"], captured.value)
    row = await store._one("SELECT * FROM agent_events WHERE id=?", (event["id"],))
    assert row["available_at"] >= time.time() + 7190
    assert await ledger.pending(session, plan) is None
    checkpoint = EventCheckpoint(store, event["id"])
    intent = [{"id": "tool-1", "function": {"name": "shell", "arguments": "submitted command"}}]
    await checkpoint.mark_tools(intent)
    await ledger.failure(event["id"], captured.value)
    row = await store._one("SELECT * FROM agent_events WHERE id=?", (event["id"],))
    assert row["status"] == "uncertain"
    assert json.loads(row["tool_intent"]) == intent


async def test_rollback_does_not_lose_matched_wait(saved, monkeypatch):
    store, session, plan = saved
    ledger = WaitLedger(store)
    await ledger.register(session, plan, registration(plan))
    for state in plan["runtime"].values():
        state["status"] = "completed"
    insert = ledger._insert

    async def interrupted(*args):
        await insert(*args)
        raise RuntimeError("crash after event insert")

    monkeypatch.setattr(ledger, "_insert", interrupted)
    with pytest.raises(RuntimeError):
        await ledger.observe(session, plan)
    assert (await ledger.rows(session, plan["id"]))[0]["status"] == "waiting"
    assert await ledger.pending(session, plan) is None
    monkeypatch.setattr(ledger, "_insert", insert)
    await ledger.observe(session, plan)
    assert await ledger.pending(session, plan)


async def test_agent_registers_wait_then_wakes_after_group_completion(cluster):
    from chemistry_agent.agent import RunProgress
    from chemistry_agent.api import create_app
    from chemistry_agent.model_runtime import ModelScope, current_model_scope

    original, _, tmp = cluster
    phase = 0
    plan_id = None

    def completion(request):
        nonlocal phase
        phase += 1
        data = json.loads(request.content)
        if phase == 1:
            args = registration({"id": plan_id}).model_dump(mode="json")
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "register",
                        "type": "function",
                        "function": {
                            "name": "calculation_wait_register",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }
        elif phase == 2:
            assert json.loads(data["messages"][-1]["content"])["status"] == "waiting"
            message = {"role": "assistant", "content": "等待两项任务完成后汇总。"}
        else:
            event = json.loads(data["messages"][-1]["content"].split("\n", 1)[1])
            assert {e["node_id"] for e in event["events"]} == {"a", "b"}
            assert event["instruction"] == "Read outputs and choose the next step"
            message = {"role": "assistant", "content": "两项任务已完成，可以分析。"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                    }
                ]
            },
        )

    app = create_app(original.chat.service.settings, transport=httpx.MockTransport(completion))
    async with app.router.lifespan_context(app):
        plans = app.state.calculation_chat.plans
        await plans.close()
        plans.slurm = LocalSlurm(plans.chat)
        session = (await plans.store.create_session("agent registers wait"))["id"]
        context = ToolContext(session, str(uuid4()))
        plan = await plans.draft(
            PlanDraft(title="group", goal="group", nodes=[job(tmp, "a"), job(tmp, "b")]), context
        )
        plan_id = plan["id"]
        run_id, history = await plans.store.begin_run(session)
        scope = current_model_scope.set(ModelScope(None))
        try:
            result = await plans.agent.run(
                [*history, {"role": "user", "content": "等这两个任务结束后分析"}],
                RunProgress(),
                session_id=session,
                run_id=run_id,
                history_count=len(history),
            )
        finally:
            current_model_scope.reset(scope)
        await plans.store.complete_run(
            session,
            run_id,
            result["messages"][len(history) :],
            result["messages"][-1]["content"],
            result["tool_calls"],
            result["usage"],
        )
        assert len(await WaitLedger(plans.store).rows(session, plan_id)) == 1
        await plans.confirm(session, plan_id, plan["revision"])
        agent, plans.agent = plans.agent, None
        await tick(plans, context, plan_id)
        await tick(plans, context, plan_id)
        plans.agent = agent
        await wake(plans, session, plan_id)
        await wake(plans, session, plan_id)
        assert phase == 3
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
        assert (await WaitLedger(plans.store).rows(session, plan_id))[0][
            "delivery_status"
        ] == "completed"


async def test_queued_user_message_has_priority(saved):
    store, session, plan = saved
    ledger = WaitLedger(store)
    plan["runtime"]["a"]["status"] = "completed"
    record_events(plan)
    await ledger.observe(session, plan)
    event = await ledger.pending(session, plan)
    async with store.transaction():
        await store.db.execute(
            "INSERT INTO chat_requests(id,session_id,body,status,created_at) "
            "VALUES(?,?,?,'queued',?)",
            (str(uuid4()), session, "{}", "now"),
        )
    with pytest.raises(AgentError, match="排队"):
        await store.begin_run(session, event_id=event["id"])
    assert (await ledger.pending(session, plan))["id"] == event["id"]


async def test_paused_and_stopped_plan_cannot_wake_agent(cluster):
    from chemistry_agent.automation.plan_authority import auto_request
    from chemistry_agent.automation.plan_stop import PlanStop

    plans, context, tmp = cluster
    store = await Store.open(tmp / "stop.sqlite3")
    session = (await store.create_session("stop"))["id"]
    context = ToolContext(session, str(uuid4()))
    plans.store = store
    try:
        plan = await plans.draft(PlanDraft(title="stop", goal="stop", nodes=[job(tmp)]), context)
        await plans.confirm(session, plan["id"], plan["revision"])
        await tick(plans, context, plan["id"])
        current = await tick(plans, context, plan["id"])
        ledger = WaitLedger(store)
        await ledger.observe(session, current)
        assert await ledger.pending(session, current)
        current["status"] = "paused"
        plans.save(session, current)
        await wake(plans, session, plan["id"])  # Provider deliberately absent: cannot be called.
        token = auto_request.set(context.run_id)
        try:
            result = await plans.stop(PlanStop(plan_id=plan["id"]), context)
        finally:
            auto_request.reset(token)
        assert result["status"] == "stopped"
        await wake(plans, session, plan["id"])
        row = await store._one("SELECT status FROM agent_events")
        assert row["status"] == "cancelled"
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 1
    finally:
        await store.close()


@pytest.mark.parametrize("phase", ["model", "tools"])
async def test_summary_retries_are_bounded_and_never_replay_uncertain_tools(saved, phase):
    store, session, plan = saved
    ledger = WaitLedger(store)
    plan["runtime"]["a"]["status"] = "completed"
    record_events(plan)
    await ledger.observe(session, plan)
    event = await ledger.pending(session, plan)
    checkpoint = EventCheckpoint(store, event["id"])
    saved_payload = {"tail": [{"role": "tool", "content": "job already submitted"}]}
    await checkpoint.save(saved_payload)
    if phase == "tools":
        await checkpoint.mark_tools([{"id": "uncertain-submit"}])
    error = AgentError(
        "context_summary_failed",
        "failed",
        503,
        cause_code="provider_rate_limited",
        retry_after=7200,
    )
    for attempt in range(4):
        await ledger.failure(event["id"], error)
        row = await store._one("SELECT * FROM agent_events WHERE id=?", (event["id"],))
        assert row["status"] == (
            "uncertain" if phase == "tools" else "blocked" if attempt == 3 else "retry_wait"
        )
        assert row["retries"] == attempt + 1
        assert row["available_at"] >= time.time() + 7190
        assert await checkpoint.load() == saved_payload
    assert await ledger.pending(session, plan) is None
