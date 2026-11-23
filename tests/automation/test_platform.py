"""The platform must operate without a model and preserve existing execution facts."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_slurm_jobs import cluster as cluster_fixture
from test_slurm_jobs import job, tick

from chemistry_agent.api import create_app
from chemistry_agent.automation.plan_models import PlanDraft
from chemistry_agent.automation.wait_ledger import WaitLedger
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import Delivery, DeliveryResult, OperationContext
from chemistry_agent.platform.lease import RuntimeLease
from chemistry_agent.schemas import ContextUsage, TokenUsage
from chemistry_agent.store import Store

cluster = cluster_fixture


def test_job_parent_requires_existing_goal_and_cannot_deadlock(tmp_path):
    child = job(tmp_path) | {"parent_task": "search"}
    with pytest.raises(ValidationError, match="parent_task"):
        PlanDraft(title="test", goal="test", nodes=[child])
    goal = {"id": "search", "title": "search", "kind": "task"}
    with pytest.raises(ValidationError, match="环"):
        PlanDraft(title="test", goal="test", nodes=[goal, child | {"depends_on": ["search"]}])
    upstream = {"id": "prepare", "title": "prepare", "kind": "task"}
    plan = PlanDraft(
        title="test",
        goal="test",
        nodes=[upstream, goal | {"depends_on": ["prepare"]}, child],
    )
    assert plan.nodes[-1].depends_on == ["prepare"]


class PythonConsumer:
    def __init__(self):
        self.events = []

    def available(self) -> bool:
        return True

    async def handle(self, delivery: Delivery) -> DeliveryResult:
        self.events.append(delivery.payload)
        return DeliveryResult(answer="Python consumer inspected the persisted completion")


async def test_pause_cancels_live_delivery_without_cancelling_scheduler_job(cluster):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "pause.sqlite3")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingConsumer(PythonConsumer):
        async def handle(self, delivery: Delivery) -> DeliveryResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    session = (await store.create_session("pause"))["id"]
    context = OperationContext(session, str(uuid4()))
    plans.store = store
    try:
        plan = await plans.platform.execute(
            "plan.save", {"title": "pause", "goal": "pause", "nodes": [job(tmp)]}, context
        )
        await plans.confirm(session, plan["id"], plan["revision"])
        await tick(plans, context, plan["id"])
        current = await tick(plans, context, plan["id"])
        current.update(status="running", delivery_adapter="waiting")
        plans.save(session, current)
        plans.dispatcher.register("waiting", WaitingConsumer())
        await plans.dispatcher.offer(session, current)
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(plans.control(session, plan["id"], "pause"), 3)
        assert cancelled.is_set()
        assert (await store._one("SELECT active_run_id FROM sessions WHERE id=?", (session,)))[
            "active_run_id"
        ] is None
        assert (await plans.control(session, plan["id"], "pause"))["status"] == "paused"
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 1
        row = await store._one("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1")
        assert row["status"] == "cancelled"
    finally:
        await store.close()


async def test_model_free_monitor_restart_and_replacement_adapter(cluster):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "platform.sqlite3")
    session = (await store.create_session("platform-only"))["id"]
    context = OperationContext(session, str(uuid4()))
    plans.store = store
    service = plans.platform
    plan = await service.execute(
        "plan.save", {"title": "job", "goal": "job", "nodes": [job(tmp)]}, context
    )
    await service.execute("plan.confirm", {"plan_id": plan["id"], "revision": 1}, context)
    await tick(plans, context, plan["id"])
    current = await tick(plans, context, plan["id"])
    assert current["status"] == "completed"
    assert plans.agent is None and plans.provider is None
    assert await WaitLedger(store).pending(session, current)
    await store.close()
    store = await Store.open(tmp / "platform.sqlite3")
    plans.store = store
    try:
        consumer = PythonConsumer()
        plans.dispatcher.register("python", consumer)
        await service.execute(
            "plan.delivery", {"plan_id": plan["id"], "adapter": "python"}, context
        )
        busy, _ = await store.begin_run(session)
        await plans.dispatcher.deliver_now(session, plan["id"], "python")
        assert not consumer.events
        await store.complete_run(session, busy, [], "user", [], TokenUsage())
        await asyncio.gather(
            *(plans.dispatcher.deliver_now(session, plan["id"], "python") for _ in range(2))
        )
        await plans.dispatcher.deliver_now(session, plan["id"], "python")
        assert len(consumer.events) == 1
        assert await WaitLedger(store).pending(session, current) is None
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 1
    finally:
        await store.close()


async def test_agent_step_many_jobs_failure_history_and_immutable_artifact(cluster, monkeypatch):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "steps.sqlite3")
    context = OperationContext((await store.create_session("research"))["id"], context.run_id)
    plans.store = store
    service = plans.platform
    plan = await service.execute(
        "plan.save",
        {
            "title": "site search",
            "goal": "choose a candidate",
            "nodes": [
                {"id": "search", "title": "find minimum", "kind": "task", "execution_host": "test"},
                {
                    "id": "followup",
                    "title": "next scientific decision",
                    "kind": "task",
                    "depends_on": ["search"],
                },
            ],
        },
        context,
    )
    await service.execute("plan.confirm", {"plan_id": plan["id"], "revision": 1}, context)
    await tick(plans, context, plan["id"])
    assert not (tmp / "scheduler.json").exists()  # The executor cannot also run agent steps.
    assert (await store._one("SELECT count(*) AS n FROM agent_events"))["n"] == 1
    original = plans.slurm.request

    async def observe(host, payload):
        if payload["action"] == "observe_external":
            identifier = payload["scheduler_job_id"]
            directory = tmp / identifier
            directory.mkdir(exist_ok=True)
            (directory / "result.bin").write_bytes(b"\x00final-data")
            identity = {
                "cluster": "lab",
                "scheduler_job_id": identifier,
                "submitted_at": "now",
                "user": "alice",
                "remote_directory": str(directory),
            }
            return {
                **identity,
                "known": True,
                "identity": identity,
                "status": "submitted",
                "terminal": True,
                "finished_ok": identifier == "43",
                "queue": "low",
                "exit_code": "0:0" if identifier == "43" else "1:0",
                "scheduler_state": "COMPLETED" if identifier == "43" else "FAILED",
            }
        return await original(host, payload)

    monkeypatch.setattr(plans.slurm, "request", observe)
    try:
        bindings = []
        for identifier in ("42", "43"):
            bindings.append(
                await service.execute(
                    "job.adopt",
                    {
                        "host": "test",
                        "scheduler_job_id": identifier,
                        "plan_id": plan["id"],
                        "step_id": "search",
                        "retrieve": ["result.bin"],
                    },
                    context,
                )
            )
        current = await tick(plans, context, plan["id"])
        assert current["status"] == "running"
        assert len(current["runtime"]["search"]["jobs"]) == 2
        assert current["runtime"][bindings[0]["node_id"]]["status"] == "failed"
        artifact = await service.execute(
            "artifact.register",
            {
                "job_id": bindings[1]["job_id"],
                "path": "result.bin",
                "key": "selected-result",
            },
            context,
        )
        (tmp / "43" / "result.bin").write_bytes(b"changed after snapshot")
        assert artifact["immutable"] is True
        assert artifact["snapshot"] is False
        assert artifact["source_state_at_capture"] == "terminal"
        content = await service.execute(
            "artifact.read", {"artifact_id": artifact["id"], "content": True}, context
        )
        assert content["content"] == "\x00final-data"
        await service.execute(
            "plan.report",
            {
                "plan_id": plan["id"],
                "node_id": "search",
                "expected_revision": current["revision"],
                "status": "completed",
                "reason": "Selected candidate 43; retained failed attempt 42",
                "accepted_jobs": [bindings[1]["job_id"]],
                "artifact_ids": [artifact["id"]],
                "result": {"selected": bindings[1]["job_id"]},
            },
            context,
        )
        current = await tick(plans, context, plan["id"])
        assert current["runtime"]["search"]["status"] == "completed"
        assert current["runtime"]["followup"]["status"] == "pending"
        assert current["runtime"][bindings[0]["node_id"]]["status"] == "failed"
        await service.execute(
            "plan.report",
            {
                "plan_id": plan["id"],
                "node_id": "followup",
                "expected_revision": current["revision"],
                "status": "completed",
                "reason": "Analysis complete; no further jobs needed",
            },
            context,
        )
        current = await tick(plans, context, plan["id"])
        assert current["status"] == "completed"
        # A downstream platform job consumes the immutable registered artifact.
        consumer = job(tmp, "consume", "cp selected.bin result.dat")
        consumer["files"] = [{"artifact_id": artifact["id"], "target": "selected.bin"}]
        submitted = await plans.draft(
            PlanDraft(title="consume", goal="consume", nodes=[consumer]), context
        )
        await plans.confirm(context.session_id, submitted["id"], 1)
        await tick(plans, context, submitted["id"])
        done = await tick(plans, context, submitted["id"])
        assert done["status"] == "completed"
        assert (
            next(
                plans.chat.directory(context.session_id, "plan_runs").glob(
                    f"{submitted['id']}/consume/attempts/*/result.dat"
                )
            ).read_bytes()
            == b"\x00final-data"
        )
    finally:
        await store.close()


def test_platform_api_without_model_and_writer_lease(settings_factory):
    settings = settings_factory(
        automation_enabled=True, platform_agent_enabled=False, deepseek_api_key=None
    )

    def no_model(request):
        pytest.fail("Platform operations must not call any model")

    app = create_app(settings, transport=httpx.MockTransport(no_model))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as client:
        with pytest.raises(AgentError, match="已有平台后台"):
            with RuntimeLease(settings):
                pytest.fail("Two active executors on one runtime must be rejected")
        session = client.post("/sessions", json={"title": "platform"}).json()["id"]
        assert "job.adopt" in client.get("/api/platform/operations").json()["operations"]
        path = f"/api/platform/{session}/operations/"
        draft = client.post(
            path + "plan.save",
            json={
                "arguments": {
                    "title": "Python controlled",
                    "goal": "external client",
                    "nodes": [{"id": "research", "title": "custom experiment", "kind": "task"}],
                }
            },
        )
        assert draft.status_code == 200, draft.text
        plan = draft.json()
        assert (
            client.post(
                path + "plan.confirm", json={"arguments": {"plan_id": plan["id"], "revision": 1}}
            ).status_code
            == 200
        )
        client.portal.call(app.state.calculation_chat.plans.tick_plan, session, plan["id"])
        assert (
            client.get(f"/api/platform/{session}/records").json()["plans"][0]["status"] == "running"
        )
        assert app.state.calculation_chat.plans.agent is None
        assert (
            client.post(
                path + "job.adopt", json={"arguments": {"host": "x", "scheduler_job_id": "bad"}}
            ).status_code
            == 422
        )
        assert client.get(f"/api/platform/{uuid4()}/records").status_code == 404
        run, _ = client.portal.call(app.state.store.begin_run, session)
        client.portal.call(
            app.state.store.complete_run,
            session,
            run,
            [{"role": "assistant", "content": "job inspected"}],
            "job inspected",
            [],
            TokenUsage(),
            ContextUsage(automation_event="job_completion"),
        )
        response = client.get(f"/sessions/{session}/runs")
        assert response.status_code == 200, response.text
        assert response.json()[0]["context"]["automation_event"] == "job_completion"
    with RuntimeLease(settings):
        pass  # Released even after an app lifecycle.


@pytest.mark.parametrize("backend", ["codex", "antigravity"])
async def test_job_completion_wakes_selected_backend_once(cluster, monkeypatch, backend):
    from types import SimpleNamespace

    from chemistry_agent.backends.service import BackendRouter, BackendSettings
    from chemistry_agent.platform.adapters.chat import ChatAgentAdapter
    from chemistry_agent.tools import ToolRegistry

    plans, context, tmp = cluster
    store = await Store.open(tmp / 'external-event.sqlite3')
    delivered = []

    class ExternalAgent:
        async def run(self, messages, progress, **kwargs):
            delivered.append((messages, kwargs))
            return {'messages': [*messages, {'role':'assistant', 'content':'已核验输出'}],
                    'tool_calls': [], 'usage': TokenUsage()}

    router = BackendRouter(store, None, ToolRegistry(), tmp, '')
    await router.initialize()
    router.register(backend, ExternalAgent())
    await router.save_settings(BackendSettings(default_backend=backend))
    session = (await store.create_session('external'))['id']
    context = OperationContext(session, str(uuid4()))
    await router.binding(session, create=True)
    plans.store, plans.agent = store, router
    plans.provider = SimpleNamespace(
        configuration=lambda: (None, SimpleNamespace(provider='unused',base_url='unused')),
        select_model=lambda model: None,
    )
    plans.dispatcher.register('chat', ChatAgentAdapter(plans))
    try:
        plan = await plans.platform.execute('plan.save',
            {'title':'job','goal':'job','nodes':[job(tmp)]}, context)
        await plans.confirm(session, plan['id'], plan['revision'])
        await tick(plans, context, plan['id'])
        current = await tick(plans, context, plan['id'])
        assert current['status'] == 'completed'
        await asyncio.gather(*plans.dispatcher.tasks_for(session, plan['id']))
        await plans.dispatcher.deliver_now(session, plan['id'], 'chat')
        await plans.dispatcher.deliver_now(session, plan['id'], 'chat')
        assert len(delivered) == 1
        assert '后台任务结束事件' in delivered[0][0][-1]['content']
        messages = (await store.get_session(session))['messages']
        assert all('后台任务结束事件' not in str(m.get('content')) for m in messages)
        assert messages[-1]['content'] == '已核验输出'
        assert len(json.loads((tmp / 'scheduler.json').read_text())) == 1
    finally:
        await store.close()
