"""Read-only browsing must avoid agent probes, transcript scans and duplicate tool blobs."""

from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.schemas import ContextUsage, TokenUsage, ToolExecution
from chemistry_agent.store import Store


def test_failed_background_run_invalidates_page_and_keeps_event_context(settings_factory):
    with TestClient(create_app(settings_factory())) as client:
        session = client.post("/sessions", json={}).json()["id"]

        async def fail() -> tuple[str, str]:
            store = client.app.state.store
            run, _ = await store.begin_run(session)
            async with store.transaction():
                await store.db.execute(
                    "UPDATE runs SET context_info=? WHERE id=?",
                    ('{"automation_event":"job_completion"}', run),
                )
            before = (await store._one("SELECT updated_at FROM sessions WHERE id=?", (session,)))[
                "updated_at"
            ]
            await store.fail_run(
                session,
                run,
                "codex_session_busy",
                tool_calls=[],
                usage=TokenUsage(),
                error_message="会话已被占用",
            )
            return run, before

        run, before = client.portal.call(fail)
        receipt = client.get(f"/sessions/{session}/chat-requests?compact=true").json()
        assert receipt["updated_at"] != before
        assert receipt["active_run_id"] is None
        result = client.get(f"/runs/{run}").json()
        assert result["status"] == "failed"
        assert result["context"]["automation_event"] == "job_completion"
        assert result["context"]["error_message"] == "会话已被占用"


def test_compact_runs_preserve_history_and_full_detail(settings_factory):
    with TestClient(create_app(settings_factory())) as client:
        session = client.post("/sessions", json={}).json()["id"]
        tool = ToolExecution(
            call_id="large",
            name="build_structure",
            arguments={"script": "a" * 500000},
            success=True,
            result={"structure_id": "example", "text": "b" * 500000},
        )

        async def seed() -> tuple[str, str]:
            store = client.app.state.store
            complete, _ = await store.begin_run(session)
            await store.complete_run(
                session,
                complete,
                [{"role": "user", "content": "构建结构"}, {"role": "assistant", "content": "完成"}],
                "完成",
                [tool],
                TokenUsage(total_tokens=123),
                context=ContextUsage(),
            )
            running, _ = await store.begin_run(session)
            async with store.transaction():
                await store.db.execute(
                    "UPDATE runs SET tool_calls=? WHERE id=?",
                    ("[" + tool.model_dump_json() + "]", running),
                )
            return complete, running

        complete, running = client.portal.call(seed)
        full = client.get(f"/sessions/{session}/runs").json()
        compact = client.get(f"/sessions/{session}/runs?compact=true").json()
        done = next(r for r in compact if r["id"] == complete)
        assert done["tool_calls"] == []
        assert done["answer"] == "完成"
        assert done["usage"]["total_tokens"] == 123
        assert next(r for r in compact if r["id"] == running)["tool_calls"] == [tool.model_dump()]
        assert next(r for r in full if r["id"] == complete)["tool_calls"] == [tool.model_dump()]
        assert client.get(f"/runs/{complete}").json()["tool_calls"] == [tool.model_dump()]
        assert len(client.get(f"/sessions/{session}").json()["messages"]) == 2


def test_external_metadata_never_parses_chat_or_probes_builtin_model(settings_factory, monkeypatch):
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
    ) as client:
        session = client.post("/sessions", json={}).json()["id"]
        assert (
            client.put(
                f"/api/agents/sessions/{session}", json={"backend": "codex", "permission": "user"}
            ).status_code
            == 200
        )

        async def forbidden(*args, **kwargs):
            raise AssertionError("Metadata must not parse transcript or probe an unrelated Agent")

        monkeypatch.setattr(Store, "_history", forbidden)
        monkeypatch.setattr(client.app.state.provider, "ready", forbidden)
        for route in (
            f"/health?session_id={session}",
            f"/api/agents/sessions/{session}",
            f"/api/agents/sessions/{session}/approvals",
            f"/api/workspaces/{session}/files",
        ):
            result = client.get(route)
            assert result.status_code == 200, result.text
        assert (
            client.get("/api/agents/sessions/00000000-0000-0000-0000-000000000000").status_code
            == 404
        )


def test_context_maintenance_does_not_invalidate_conversation(settings_factory):
    with TestClient(create_app(settings_factory())) as client:
        session = client.post("/sessions", json={}).json()["id"]
        before = client.get(f"/sessions/{session}").json()

        async def maintain() -> None:
            store = client.app.state.store
            run, _ = await store.begin_run(session)
            await store.complete_run(
                session,
                run,
                [],
                "",
                [],
                TokenUsage(),
                context=ContextUsage(manual_compaction=True),
                touch_session=False,
            )

        client.portal.call(maintain)
        after = client.get(f"/sessions/{session}").json()
        assert before == after
