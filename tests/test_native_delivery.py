"""Both native protocols obey the same durable event dispatch boundary."""

import time

import pytest
from test_agent_backends import fake_codex as codex_fixture
from test_antigravity_backend import fake_acp as acp_fixture

from chemistry_agent.agent import RunProgress
from chemistry_agent.automation.wait_ledger import EventCheckpoint, WaitLedger
from chemistry_agent.backends.service import BackendRouter, BackendSettings
from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.checkpoint import current_checkpoint
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolRegistry

fake_codex = codex_fixture
fake_acp = acp_fixture


@pytest.mark.parametrize("backend", ["codex", "antigravity"])
async def test_preflight_event_retry_and_completed_replay_after_restart(
    settings_factory, tmp_path, monkeypatch, fake_codex, fake_acp, backend
):
    database = settings_factory().database_path

    async def open_router():
        store = await Store.open(database)
        router = BackendRouter(store, None, ToolRegistry(), tmp_path / "workspaces", "")
        await router.initialize()
        return router

    router = await open_router()
    fake = fake_codex if backend == "codex" else fake_acp
    method_name = "thread/resume" if backend == "codex" else "session/load"
    native = "external-thread" if backend == "codex" else "native-session"
    original_request = fake.request
    blocked = True

    async def request(self, method, params, **kwargs):
        if method == method_name and blocked:
            raise AgentError(f"{backend}_session_busy", "native occupied", 409)
        return await original_request(self, method, params, **kwargs)

    monkeypatch.setattr(fake, "request", request)
    token = None
    try:
        await router.save_settings(BackendSettings(default_backend=backend))
        session = (await router.store.create_session("event recovery"))["id"]
        await router.binding(session, create=True)
        await router.update_thread(session, external_thread_id=native)
        async with router.store.transaction():
            await router.store.db.execute(
                "INSERT INTO agent_events(id,session_id,plan_id,payload,status,created_at,phase) "
                "VALUES('event',?,'plan','{}','running',?,'tools')",
                (session, time.time()),
            )
        token = current_checkpoint.set(EventCheckpoint(router.store, "event"))
        kwargs = {"session_id": session, "run_id": "try1", "history_count": 0}
        messages = [{"role": "user", "content": "后台任务结束事件：核验结果"}]
        with pytest.raises(AgentError) as error:
            await router.run(messages, RunProgress(), **kwargs)
        assert error.value.delivery_not_sent
        await WaitLedger(router.store).failure("event", error.value)
        row = await router.store._one("SELECT * FROM agent_events WHERE id='event'", ())
        assert row["phase"] == "model" and row["status"] == "retry_wait"
        assert (fake.starts if backend == "codex" else fake.prompts) == 0
        blocked = False
        result = await router.run(messages, RunProgress(), **{**kwargs, "run_id": "try2"})
        assert result["messages"][-1]["content"] == "甲醇 CH4O"
        await router.store.close()
        router = await open_router()
        current_checkpoint.reset(token)
        token = current_checkpoint.set(EventCheckpoint(router.store, "event"))
        cached = await router.run(messages, RunProgress(), **{**kwargs, "run_id": "try3"})
        assert cached["messages"] == result["messages"]
        assert (fake.starts if backend == "codex" else fake.prompts) == 1
        assert (await router.binding(session))["external_thread_id"] == native
    finally:
        if token is not None:
            current_checkpoint.reset(token)
        await router.store.close()
