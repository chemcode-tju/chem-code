"""Persistence, background transitions and bounded recovery, without real remote jobs."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.events import EventStore, task_state
from chemistry_agent.automation.monitor import TaskMonitor
from chemistry_agent.automation.recovery import (
    RecoveryAgent,
    RecoveryDecision,
    RecoveryPolicy,
    apply_recovery,
    safe_to_replace,
)
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


def base_preview() -> dict:
    return {
        "structure_id": str(uuid4()),
        "request": {
            "kind": "vasp",
            "server_id": str(uuid4()),
            "code_id": str(uuid4()),
            "structure": {"content": "unused"},
            "incar": {"ENCUT": 520, "NSW": 0},
            "resources": {"queue": "test", "wallclock_seconds": 3600},
            "potential_family": "test",
            "potential_mapping": {"Cu": "Cu"},
        },
    }


@pytest.mark.parametrize(
    "updates", [{"ENCUT": 300}, {"NELM": 241}, {"NELM": True}, {"ALGO": "Fast"}]
)
def test_recovery_rejects_unauthorized_changes(updates):
    with pytest.raises(AgentError):
        apply_recovery(
            base_preview(),
            RecoveryDecision(action="retry", reason="test", incar=updates),
            RecoveryPolicy(),
        )


def test_recovery_bounds_do_not_expand_with_each_attempt():
    preview = base_preview()
    preview["recovery_baseline_resources"] = {"wallclock_seconds": 3600}
    preview["request"]["resources"]["wallclock_seconds"] = 7200
    with pytest.raises(AgentError):
        apply_recovery(
            preview,
            RecoveryDecision(action="retry", reason="test", wallclock_seconds=10000),
            RecoveryPolicy(),
        )
    preview["recovery_attempt"] = 2
    with pytest.raises(AgentError):
        apply_recovery(preview, RecoveryDecision(action="retry", reason="test"), RecoveryPolicy())
    assert not safe_to_replace({"process_state": "waiting", "scheduler_job_id": "1"})
    assert not safe_to_replace(
        {"process_state": "waiting", "submission_state": "rejected_by_scheduler"}
    )


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


def recovery_fixture(tmp_path, settings_factory, mode="auto", hook=None):
    service = FakeService(settings_factory())
    chat = CalculationChat(service)
    store = EventStore(tmp_path / "events.db")
    session, preview_id = str(uuid4()), str(uuid4())
    chat.save(session, "previews", preview_id, base_preview())
    store.set_policy(service.job["id"], {"mode": mode, "max_attempts": 2})
    store.enqueue(session, preview_id, service.job["id"])

    async def preview(args, context):
        identifier = str(uuid4())
        record = base_preview()
        record["request"] = args.model_dump(mode="json")
        chat.save(context.session_id, "previews", identifier, record)
        return {"preview_id": identifier}

    chat.preview = preview
    agent = RecoveryAgent(chat, store, FakeProvider(hook))
    return agent, store, service


@pytest.mark.parametrize(
    "mode,expected", [("auto", "submitted"), ("diagnose", "awaiting_confirmation")]
)
async def test_model_diagnosis_obeys_policy(tmp_path, settings_factory, mode, expected):
    agent, store, service = recovery_fixture(tmp_path, settings_factory, mode)
    await agent.process(store.next_recovery())
    result = store.recovery(service.job["id"])
    assert result["state"] == expected
    assert result["result"]["preview_id"]
    assert sum(action == "submit" for action, _ in service.calls) == (mode == "auto")
    assert store.next_recovery() is None


async def test_policy_disabled_during_diagnosis(tmp_path, settings_factory):
    agent, store, service = recovery_fixture(tmp_path, settings_factory)
    agent.provider.hook = lambda: store.set_policy(service.job["id"], {"mode": "off"})
    await agent.process(store.next_recovery())
    assert store.recovery(service.job["id"])["state"] == "disabled"
    assert not any(action == "submit" for action, _ in service.calls)


async def test_uncertain_submit_keeps_exact_preview_and_never_repeats(tmp_path, settings_factory):
    agent, store, service = recovery_fixture(tmp_path, settings_factory)
    service.uncertain = True
    await agent.process(store.next_recovery())
    result = store.recovery(service.job["id"])
    assert result["state"] == "needs_user"
    assert result["result"]["preview_id"]
    store.enqueue(result["session_id"], result["preview_id"], service.job["id"], force=True)
    assert store.next_recovery() is None
    assert EventStore(store.path).next_recovery() is None


async def test_monitor_updates_without_browser_and_does_not_retry_history(settings_factory):
    service = FakeService(settings_factory())
    chat = CalculationChat(service)
    session, preview_id = str(uuid4()), str(uuid4())
    chat.save(session, "previews", preview_id, base_preview())
    chat.save(session, "submissions", preview_id, {"job": service.job})
    monitor = TaskMonitor(chat, FakeProvider())
    await monitor.tick()
    assert monitor.events.inbox(session)["unread"] == 1
    assert monitor.events.next_recovery() is None
    service.job.update(process_state="waiting", scheduler_job_id="7")
    await monitor.tick()
    service.job.update(process_state="finished", exit_status=1)
    await monitor.tick()
    assert monitor.events.next_recovery()["job_id"] == service.job["id"]
    await monitor.tick()
    assert monitor.events.inbox(session)["unread"] == 1
    assert monitor.events.inbox(session)["monitor"]["last_success"]


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
        assert (
            client.post(
                f"/api/automation/notifications/{event_id}/read",
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )


async def test_background_diagnosis_uses_saved_session_model(tmp_path, settings_factory):
    from chemistry_agent.model_settings import ModelSettings

    agent, store, service = recovery_fixture(tmp_path, settings_factory, mode="diagnose")
    preferences = []
    choices = []

    class SessionStore:
        async def get_session_model(self, session, owner, provider, base_url):
            preferences.append((session, owner, provider, base_url))
            return "saved-session-model"

    agent.session_store = SessionStore()
    configuration = ModelSettings(provider="openai-compatible", model="default-model")
    agent.provider.configuration = lambda: (None, configuration)

    def select_model(model):
        choices.append(model)
        return configuration.model_copy(update={"model": model})

    agent.provider.select_model = select_model
    await agent.process(store.next_recovery())
    assert choices == ["saved-session-model"]
    assert preferences[0][1:] == (None, "openai-compatible", "https://api.deepseek.com")
    record = store.recovery(service.job["id"])
    assert record["decision"]["model"] == {
        "provider": "openai-compatible",
        "model": "saved-session-model",
    }
    assert configuration.model == "default-model"
