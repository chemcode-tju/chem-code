"""A waiting task must leave other execution sessions available."""

import asyncio
from uuid import uuid4

import pytest
from test_slurm_jobs import cluster as cluster_fixture

from chemistry_agent.automation.job_events import record_events
from chemistry_agent.automation.plan_models import PlanDraft
from chemistry_agent.automation.wait_ledger import WaitLedger
from chemistry_agent.backends.service import BackendRouter
from chemistry_agent.platform import dispatch
from chemistry_agent.platform.contracts import Delivery, DeliveryResult, OperationContext
from chemistry_agent.platform.task_agents import current_task_agent, ensure_session, task_session
from chemistry_agent.schemas import TokenUsage
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolRegistry

cluster = cluster_fixture


@pytest.mark.parametrize("blockage", ["approval", "busy", "capacity"])
async def test_dispatch_other_task_while_first_is_blocked(cluster, monkeypatch, blockage):
    plans, _, tmp = cluster
    if blockage == "capacity":
        monkeypatch.setattr(dispatch, "MAX_BACKGROUND_RUNS", 1)
    preset = {"backend": "codex", "instance_id": "local-codex", "model": "fixture", "effort": ""}
    delivered = []
    deliveries = {}
    approval_requested = asyncio.Event()
    approval_answered = asyncio.Event()
    second_started = asyncio.Event()
    approval_cancelled = asyncio.Event()

    class Consumer:
        def available(self) -> bool:
            return True

        async def handle(self, delivery: Delivery) -> DeliveryResult:
            task = current_task_agent.get()
            assert task is not None
            assert [item["node_id"] for item in delivery.payload["events"]] == [task.node_id]
            assert delivery.session_id == task.session_id
            delivered.append(task.node_id)
            deliveries[task.node_id] = asyncio.current_task()
            if task.node_id == "review":
                approval_requested.set()
                try:
                    await approval_answered.wait()
                finally:
                    approval_cancelled.set()
            else:
                second_started.set()
            return DeliveryResult(answer="other task")

    store = await Store.open(tmp / "dispatch.sqlite3")
    plans.store = store
    router = BackendRouter(store, None, ToolRegistry(), tmp / "workspaces", "")
    await router.initialize()
    plans.agent = router
    parent = (await store.create_session("parent"))["id"]
    context = OperationContext(parent, str(uuid4()))
    busy_run = None
    try:
        plan = await plans.draft(
            PlanDraft(
                title="parallel tasks",
                goal="independent results",
                nodes=[
                    {"id": key, "title": key, "kind": "task"}
                    for key in ("review", "analyze")
                ],
            ),
            context,
        )
        plan_id = plan["id"]
        await plans.confirm(parent, plan_id, plan["revision"])
        plan = plans.chat.load(parent, "plans", plan_id)
        plan.update(
            delivery_adapter="fixture", task_agents=dict.fromkeys(("review", "analyze"), preset)
        )
        plans.dispatcher.register("fixture", Consumer())
        plan["runtime"]["analyze"]["status"] = "running"
        record_events(plan)
        plans.save(parent, plan)
        ledger = WaitLedger(store)
        await ledger.observe(parent, plan)
        if blockage != "busy":
            await plans.dispatcher.offer(parent, plan)
            await asyncio.wait_for(approval_requested.wait(), 2)
        else:
            target = await ensure_session(router, parent, plan, "review", None)
            event = await ledger.pending(parent, plan)
            busy_run, _ = await store.begin_run(
                target, event_id=event["id"], event_session_id=parent
            )
            # A subsequent ready event stays ahead of the independent task in the same plan.
            plan["runtime"]["review"]["generic_job_id"] = "next-review-attempt"
            plan["runtime"]["review"]["attempts"] = [{}]
            record_events(plan)
            await ledger.observe(parent, plan)

        plan["runtime"]["analyze"]["status"] = "pending"
        record_events(plan)
        plans.save(parent, plan)
        await ledger.observe(parent, plan)
        await asyncio.gather(*(plans.dispatcher.offer(parent, plan) for _ in range(2)))
        if blockage == "capacity":
            assert plans.dispatcher.tasks_for(parent, plan_id) == {deliveries["review"]}
            assert not second_started.is_set()
            approval_answered.set()
            await asyncio.wait_for(deliveries["review"], 2)
            await plans.dispatcher.offer(parent, plan)
        await asyncio.wait_for(second_started.wait(), 2)
        await asyncio.wait_for(deliveries["analyze"], 2)
        user_run, _ = await store.begin_run(parent)
        await store.complete_run(parent, user_run, [], "interactive", [], TokenUsage())
        if blockage == "approval":
            assert not approval_cancelled.is_set()
            await plans.control(parent, plan_id, "pause")
            assert approval_cancelled.is_set()
        assert delivered == (["analyze"] if blockage == "busy" else ["review", "analyze"])
        analyze = task_session(parent, plan_id, "analyze")
        assert [run["status"] for run in await store.list_runs(analyze, 20, 0)] == ["completed"]
    finally:
        await plans.dispatcher.close()
        if busy_run:
            await store.complete_run(target, busy_run, [], "previous review", [], TokenUsage())
        await store.close()
