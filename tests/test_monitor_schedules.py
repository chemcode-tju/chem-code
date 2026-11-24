"""Scheduled turns share real chat receipts. Only the model transport is simulated."""

import asyncio
import json
from uuid import uuid4

import httpx

from chemistry_agent.api import create_app
from chemistry_agent.automation.monitor_schedules import tick

BASE = "/api/job-monitor/wakes"


async def test_schedule_catchup_overlap_and_controls(settings_factory):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def provider(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"][-1]["content"])
        started.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "已检查"}, "finish_reason": "stop"}
                ]
            },
        )

    settings = settings_factory(automation_enabled=True)
    app = create_app(settings, transport=httpx.MockTransport(provider))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        session = (await client.post("/sessions", json={})).json()["id"]
        path = f"{BASE}/schedules/{uuid4()}"
        spec = dict(
            name="进度",
            instruction="核对原始输出",
            session_id=session,
            interval_minutes=2,
            enabled=True,
        )
        assert (await client.put(path, json=spec | {"interval_minutes": 0})).status_code == 422
        assert (await client.put(path, json=spec | {"session_id": str(uuid4())})).status_code == 404
        assert (
            await client.put(path, headers={"origin": "https://example.org"}, json=spec)
        ).status_code == 403
        assert (await client.put(path, json=spec)).status_code == 200
        row = (await client.get(BASE)).json()["schedules"][0]
        now = row["next_at"] + 12 * 120 + 7
        await tick(app, now)
        await asyncio.wait_for(started.wait(), 5)
        assert "定时任务「进度」" in calls[0] and "核对原始输出" in calls[0]
        await tick(app, now + 120)
        row = (await client.get(BASE)).json()["schedules"][0]
        assert row["last_status"] == "running" and row["next_at"] == now + 240 - 7
        assert len((await client.get(f"/sessions/{session}/chat-requests")).json()["requests"]) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
        assert (await client.put(path, json=spec | {"enabled": False})).is_success
        await tick(app, now + 500)
        assert len(calls) == 1
        assert (
            await client.put(f"{BASE}/defaults", json={"instruction": "核对能量与力"})
        ).is_success
        assert (
            await client.put(path, json=spec | {"name": "新标题", "interval_minutes": 5})
        ).is_success

    restarted = create_app(settings)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1"
        ) as client,
    ):
        data = (await client.get(BASE)).json()
        assert data["instruction"] == "核对能量与力"
        row = data["schedules"][0]
        assert row["name"] == "新标题" and row["interval_minutes"] == 5 and row["enabled"]
        assert row["last_status"] == "completed"
        assert (await client.delete(path)).is_success
        assert not (await client.get(BASE)).json()["schedules"]


async def test_restart_between_timer_commit_and_dispatch(settings_factory, monkeypatch):
    settings = settings_factory(automation_enabled=True)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        session = (await client.post("/sessions", json={})).json()["id"]
        path = f"{BASE}/schedules/{uuid4()}"
        spec = dict(
            name="离线检查",
            instruction="检查并汇总",
            session_id=session,
            interval_minutes=5,
            enabled=True,
        )
        assert (await client.put(path, json=spec)).is_success
        due = (await client.get(BASE)).json()["schedules"][0]["next_at"]

        async def defer(*args) -> None:
            pass

        monkeypatch.setattr(app.state.chat_queue, "recover", defer)
        await tick(app, due + 5 * 300)
        receipt = (await client.get(BASE)).json()["schedules"][0]
        assert receipt["last_status"] == "queued" and receipt["next_at"] == due + 6 * 300

    calls = []

    async def provider(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "已汇总"}, "finish_reason": "stop"}
                ]
            },
        )

    restarted = create_app(settings, transport=httpx.MockTransport(provider))
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1"
        ) as client,
    ):
        await asyncio.wait_for(asyncio.gather(*restarted.state.chat_queue.tasks), 5)
        row = (await client.get(BASE)).json()["schedules"][0]
        assert row["last_request"] == receipt["last_request"] and row["last_status"] == "completed"
        assert len(calls) == 1


async def test_overdue_schedule_wakes_original_chat_on_startup_without_browser(settings_factory):
    settings = settings_factory(automation_enabled=True, platform_agent_enabled=False)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        session = (await client.post("/sessions", json={})).json()["id"]
        identifier = str(uuid4())
        spec = dict(
            name="监控",
            instruction="核查计算进度",
            session_id=session,
            interval_minutes=10,
            enabled=True,
        )
        assert (await client.put(f"{BASE}/schedules/{identifier}", json=spec)).is_success
        async with app.state.store.transaction():
            await app.state.store.db.execute(
                "UPDATE monitor_schedules SET next_at=100 WHERE id=?", (identifier,)
            )

    delivered = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        delivered.set()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "收到进度检查"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = create_app(
        settings.model_copy(update={"platform_agent_enabled": True}),
        transport=httpx.MockTransport(provider),
    )
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(delivered.wait(), 10)
        await asyncio.gather(*app.state.chat_queue.tasks)
        history = await app.state.store.get_session(session)
        assert [m["content"] for m in history["messages"]] == [
            "定时任务「监控」\n核查计算进度",
            "收到进度检查",
        ]


async def test_agent_creates_without_plan_and_controls_only_own_conversation(settings_factory):
    from chemistry_agent.provider import ToolCall
    from chemistry_agent.tools import ToolContext

    spec = dict(key="cu-check", name="界面进度", instruction="核对输出", interval_minutes=30)

    def call(name: str, args: dict) -> dict:
        return {
            "id": str(uuid4()),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def model(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "monitor_schedule_set" in {t["function"]["name"] for t in payload["tools"]}
        if payload["messages"][-1]["role"] == "tool":
            message = {"role": "assistant", "content": "定时检查已登记。"}
            reason = "stop"
        else:
            message = {"role": "assistant", "tool_calls": [call("monitor_schedule_set", spec)]}
            reason = "tool_calls"
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": reason}]}
        )

    app = create_app(
        settings_factory(automation_enabled=True), transport=httpx.MockTransport(model)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        session = (await client.post("/sessions", json={})).json()["id"]
        other = (await client.post("/sessions", json={})).json()["id"]
        answer = await client.post(
            "/chat", json={"session_id": session, "message": "每半小时检查本对话作业"}
        )
        assert answer.json()["tool_calls"][0]["success"], answer.text
        (timer,) = (await client.get(BASE)).json()["schedules"]
        assert timer["created_by"] == "agent" and timer["session_id"] == session

        async def invoke(session_id: str, name: str, args: dict):
            return await app.state.agent.registry.execute(
                ToolCall.model_validate(call(name, args)), ToolContext(session_id, str(uuid4()))
            )

        save, control = "monitor_schedule_set", "monitor_schedule_control"
        repeated = await invoke(session, save, spec)
        assert (
            repeated.result["id"] == timer["id"] and repeated.result["next_at"] == timer["next_at"]
        )
        assert not (await invoke(other, control, {"action": "list"})).result["schedules"]
        target = {"schedule_id": timer["id"]}
        assert not (await invoke(other, control, target | {"action": "delete"})).success
        changed = {k: v for k, v in spec.items() if k != "key"} | target | {"interval_minutes": 45}
        assert not (await invoke(other, save, changed)).success
        assert (await invoke(session, control, target | {"action": "pause"})).success
        stale = await client.put(
            f"{BASE}/schedules/{timer['id']}?revision={timer['revision']}",
            json={
                "session_id": session,
                "name": "旧表单",
                "interval_minutes": 5,
                "enabled": True,
                "instruction": "旧指令",
            },
        )
        assert stale.status_code == 409

        assert not (await invoke(session, save, changed)).result["enabled"]
        assert (await invoke(session, control, target | {"action": "resume"})).success
        assert (await invoke(session, control, target | {"action": "delete"})).success
        assert not (await client.get(BASE)).json()["schedules"]


async def test_workflow_panel_includes_existing_plans_and_explicit_waits(settings_factory):
    from chemistry_agent.automation.wait_ledger import WaitLedger
    from chemistry_agent.automation.wait_models import WaitRegister

    app = create_app(settings_factory(automation_enabled=True))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        session = (await client.post("/sessions", json={"title": "Cu(100)"})).json()["id"]
        other = (await client.post("/sessions", json={})).json()["id"]
        plan = {
            "id": str(uuid4()),
            "title": "吸附构型",
            "status": "paused",
            "goal": "检查",
            "nodes": [{"id": "relax", "title": "结构优化", "kind": "job"}],
            "runtime": {
                "relax": {
                    "status": "running",
                    "job": {"scheduler_job_id": "42", "scheduler_state": "RUNNING"},
                }
            },
        }
        chat = app.state.calculation_chat
        chat.save(session, "plans", plan["id"], plan)
        await WaitLedger(app.state.store).register(
            session,
            plan,
            WaitRegister(
                plan_id=plan["id"],
                key="wait-relax",
                instruction="验证力收敛",
                condition={"op": "state", "node": "relax", "states": ["completed"]},
            ),
        )
        assert (await client.get("/monitor/tasks")).is_success
        (row,) = (await client.get(f"{BASE}/workflows")).json()["plans"]
        assert row["session_title"] == "Cu(100)" and row["status"] == "paused"
        assert row["nodes"][0]["scheduler_state"] == "RUNNING"
        assert (
            row["waits"][0]["instruction"] == "验证力收敛"
            and row["waits"][0]["status"] == "waiting"
        )
        assert not (await client.get(f"{BASE}/workflows?session_id={other}")).json()["plans"]
        plan["title"] = "新版方案"
        chat.save(session, "plans", plan["id"], plan)
        assert (await client.get(f"{BASE}/workflows?session_id={session}")).json()["plans"][0][
            "title"
        ] == "新版方案"
