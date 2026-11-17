"""Persistence, background transitions and bounded recovery, without real remote jobs."""

import json
from types import SimpleNamespace
from uuid import uuid4

from chemistry_agent.automation.events import EventStore, task_state
from chemistry_agent.errors import AgentError
from chemistry_agent.provider import AssistantMessage, Completion
from chemistry_agent.schemas import TokenUsage


def test_status_convergence_and_child_failure():
    job = {
        "finished_ok": True,
        "results": {
            "run_status": {"electronic_converged": True, "ionic_converged": False, "nsw": 0}
        },
    }
    assert task_state(job)[0] == "completed"
    job["results"]["run_status"]["nsw"] = 50
    assert task_state(job)[0] == "unconverged"
    del job["results"]["run_status"]["ionic_converged"]
    assert task_state(job)[0] == "needs_review"
    assert task_state({"finished_ok": True})[0] == "needs_review"
    assert (
        task_state(
            {
                "workflow": "relax_static",
                "steps": [{"pk": 5, "paused": True, "submission_state": "rejected_by_scheduler"}],
            }
        )[0]
        == "submission_rejected"
    )


def test_durable_deduplicated_notifications_and_budget(tmp_path):
    path = tmp_path / "events.db"
    store = EventStore(path)
    job = {"id": "j", "pk": 1, "process_state": "waiting", "scheduler_job_id": "99"}
    assert store.observe("s", "p", job)
    assert store.inbox("s")["unread"] == 0
    job.update(
        process_state="finished",
        finished_ok=True,
        results={"run_status": {"electronic_converged": True}},
    )
    assert store.observe("s", "p", job)
    store = EventStore(path)
    assert not store.observe("s", "p", job)
    assert store.pending_reports() == []  # explicitly suppressed, including state transitions
    assert store.inbox("s")["unread"] == 1
    store.mark_read(store.inbox("s")["events"][0]["id"])
    assert EventStore(path).inbox("s")["unread"] == 0
    assert store.reserve_retry("root", "first", "p1", 2)
    assert store.reserve_retry("root", "first", "p1", 2)
    assert not store.reserve_retry("root", "first", "other", 2)
    assert store.reserve_retry("root", "second", "p2", 2)
    assert not store.reserve_retry("root", "branch", "p3", 2)
    store.enqueue("s", "p", "j")
    store.update_recovery("j", "running")
    assert EventStore(path).recovery("j")["state"] == "interrupted"
    store.enqueue("s", "p", "j", force=True)
    assert store.next_recovery()["state"] == "queued"


class FakeService:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.job = {"id": str(uuid4()), "pk": 12, "process_state": "finished", "exit_status": 1}
        self.uncertain = False

    async def call(self, action, payload=None):
        self.calls.append((action, payload))
        if action == "tracked_jobs":
            return {"jobs": [self.job]}
        if action == "job_details":
            return self.job
        if action == "submit":
            if self.uncertain:
                raise AgentError("submission_uncertain", "提交结果待核查", 409)
            return {"job": {"id": str(uuid4()), "pk": 13}, "reused": False}
        raise AssertionError(action)


class FakeProvider:
    def __init__(self, hook=None):
        self.hook = hook
        self.calls = 0

    async def ready(self):
        return True

    async def complete(self, messages, tools, **kwargs):
        assert not tools and kwargs["allow_tools"] is False
        self.calls += 1
        if self.hook:
            self.hook()
        return Completion(
            message=AssistantMessage(
                role="assistant",
                content=json.dumps({"action": "retry", "reason": "已证实节点暂时故障，原参数重试"}),
            ),
            usage=TokenUsage(),
        )


def test_event_and_agent_wakeup_survive_restart_together(tmp_path):
    store = EventStore(tmp_path / "events.db")
    job = {"id": "j", "pk": 1, "process_state": "excepted"}
    store.observe("session", "preview", job, diagnose=True)
    store = EventStore(store.path)
    assert store.inbox("session")["unread"] == 1
    assert store.next_recovery()["job_id"] == "j"
    assert not store.observe("session", "preview", job, diagnose=True)


def test_notification_routes_respect_local_boundary(settings_factory, tmp_path):
    from fastapi.testclient import TestClient

    from chemistry_agent.api import create_app

    app = create_app(settings_factory(automation_enabled=True))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as client:
        store = EventStore(tmp_path / "events.db")
        app.state.task_monitor = SimpleNamespace(events=store)
        store.notify("session", "job", "completed", "test", {}, "unique")
        data = client.get("/api/automation/notifications").json()
        assert data["unread"] == 1
        event_id = data["events"][0]["id"]
        response = client.post(f"/api/automation/notifications/{event_id}/read")
        assert response.status_code == 200
        assert client.get("/api/automation/notifications").json()["unread"] == 0
        for i in range(3):
            store.notify("second-session", "other", "failed", "failed", {}, f"failed-{i}")
        page = client.get("/api/automation/notifications?limit=1").json()
        assert page["totals"] == {"normal": 1, "exception": 3}
        assert {row["session_id"] for row in page["events"]} == {"session", "second-session"}
        assert (
            client.post(
                "/api/automation/notifications/read-all", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert client.post("/api/automation/notifications/read-all").status_code == 200
        assert client.get("/api/automation/notifications").json()["unread"] == 0
        assert (
            client.post(
                f"/api/automation/notifications/{event_id}/read",
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )


async def test_report_is_an_assistant_message_once_without_prompt_or_interleaving(tmp_path):
    from types import SimpleNamespace

    from chemistry_agent.automation.reporting import RESULT_PROMPT, deliver_pending
    from chemistry_agent.store import Store

    session_store = await Store.open(tmp_path / "chat.sqlite3")
    try:
        session = (await session_store.create_session("CO"))["id"]
        events = EventStore(tmp_path / "monitor.sqlite3")
        job = {
            "id": "job",
            "pk": 123,
            "finished_ok": True,
            "process_state": "finished",
            "results": {"run_status": {"electronic_converged": True}},
        }
        events.observe(session, "draft", job, report=True)

        class Provider(FakeProvider):
            def configuration(self):
                return None, SimpleNamespace(provider="chatgpt", base_url="")

            def select_model(self, model):
                pass

            async def complete(self, messages, tools, **kwargs):
                self.calls += 1
                assert messages[0]["content"] == RESULT_PROMPT
                assert not tools and not kwargs["allow_tools"]
                return SimpleNamespace(
                    message=SimpleNamespace(content="电子已收敛。建议比较参考能量。", tool_calls=[])
                )

        provider = Provider()
        monitor = SimpleNamespace(
            events=events, recovery=SimpleNamespace(provider=provider), session_store=session_store
        )
        run, _ = await session_store.begin_run(session)
        await deliver_pending(monitor)
        assert not (await session_store.get_session(session))["messages"]
        await session_store.fail_run(session, run, "test", tool_calls=[], usage=TokenUsage())
        await deliver_pending(monitor)
        events.observe(session, "draft", job, report=True)
        await deliver_pending(monitor)
        history = (await session_store.get_session(session))["messages"]
        assert len(history) == 1 and history[0]["role"] == "assistant"
        assert history[0]["task_event"] == events.latest_report(session)
        assert "建议" in history[0]["content"] and RESULT_PROMPT not in str(history)
        _, hidden_free = await session_store.begin_run(session)
        assert "task_event" not in hidden_free[0]
        assert provider.calls == 1
    finally:
        await session_store.close()


async def test_legacy_children_and_retry_are_progress_until_root_ends(tmp_path):
    from chemistry_agent.automation.reporting import deliver_pending
    from chemistry_agent.store import Store

    db = tmp_path / "monitor.db"
    events = EventStore(db)
    sessions = await Store.open(tmp_path / "chat.db")
    try:
        sid = (await sessions.create_session("legacy"))["id"]
        first = {
            "id": "root",
            "pk": 4,
            "workflow": "relax_static",
            "process_state": "waiting",
            "steps": [
                {
                    "id": "child",
                    "pk": 5,
                    "process_state": "finished",
                    "finished_ok": True,
                    "results": {"run_status": {"electronic_converged": True}},
                }
            ],
        }
        events.observe(sid, "p1", first, report=True)
        failed = first | {"process_state": "finished", "exit_status": 1}
        events.observe(sid, "p1", failed, report=True, diagnose=True)
        events.update_recovery("root", "submitted", result={"reason": "已提交重试"})
        for row in events.pending_reports():
            events.save_report(row["event_key"], "原作业失败，修复已提交")
        monitor = SimpleNamespace(events=events, session_store=sessions)
        await deliver_pending(monitor)
        assert not (await sessions.get_session(sid))["messages"]
        last = {
            "id": "retry",
            "pk": 6,
            "recovery_root_id": "root",
            "process_state": "finished",
            "finished_ok": True,
            "results": {"run_status": {"electronic_converged": True}},
        }
        events.observe(sid, "p2", last, report=True)
        events.save_report(events.pending_reports()[0]["event_key"], "完成")
        await deliver_pending(monitor)
        events.observe(sid, "p2", last, report=True)
        await deliver_pending(monitor)
        assert [m["content"] for m in (await sessions.get_session(sid))["messages"]] == ["完成"]
        fresh = EventStore(db)
        one = fresh.timeline(sid, 0, 2, "root")
        assert one["has_more"]
        rest = fresh.timeline(sid, one["next_cursor"], 100, "root")
        all_rows = one["events"] + rest["events"]
        assert len(all_rows) == len({e["seq"] for e in all_rows})
        assert len([e for e in all_rows if e["kind"] == "plan"]) == 1
        assert len(fresh.timeline(sid, 0, 100, "root", "child")["events"]) == 1
        assert fresh.timeline("other", 0, 100)["events"] == []
    finally:
        await sessions.close()


async def test_upgrade_routes_pending_reviewed_plan_report_to_node(tmp_path):
    from chemistry_agent.automation.reporting import deliver_pending

    events = EventStore(tmp_path / "monitor.sqlite3")
    job = {"id": "job", "pk": 10, "process_state": "finished", "finished_ok": False}
    events.observe("session", "preview", job, report=True)
    events.save_report(events.pending_reports()[0]["event_key"], "旧版保存的解读")
    chat = SimpleNamespace(load=lambda *_: {"plan_id": "plan", "plan_node": "relax"})
    monitor = SimpleNamespace(chat=chat, events=events)
    await deliver_pending(monitor)
    await deliver_pending(monitor)
    rows = events.timeline("session", 0, 10, "plan", "relax")["events"]
    assert [(r["kind"], r["message"]) for r in rows] == [("interpretation", "旧版保存的解读")]
    assert not events.latest_report("session")
