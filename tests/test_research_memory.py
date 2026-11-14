"""Research reuse, provenance, authorization and failure chains without real cluster jobs."""

import copy
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.auth import create_user_account
from chemistry_agent.errors import AgentError
from chemistry_agent.memory.capture import (
    calculation_evidence,
    capture_document,
    capture_job,
    capture_plan,
)
from chemistry_agent.memory.diagnostics import diagnostic_references
from chemistry_agent.memory.models import MemoryDocument, MemorySearch, parameter_diff
from chemistry_agent.memory.service import ResearchMemory
from chemistry_agent.store import Store
from chemistry_agent.tokens import TokenCounter


def document(**values):
    return MemoryDocument(
        **(
            {
                "title": "CO/Pt(111) 吸附能计算方案",
                "kind": "plan",
                "content": "以优化后的洁净表面为基准；气相 CO 单独计算。来源：方案测试夹具。",
                "systems": ["Pt(111)", "CO"],
                "tasks": ["吸附能"],
                "parameters": {"incar": {"ENCUT": 520, "ISPIN": 1}, "kmesh": [4, 4, 1]},
            }
            | values
        )
    )


def response(request):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "已读取相关记录。",
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )


async def test_same_system_different_task_same_task_different_system_and_restart(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = await Store.open(path)
    session = (await store.create_session("原始计算"))["id"]
    second = (await store.create_session("新任务"))["id"]
    memory = ResearchMemory(store)
    item = await memory.save(session, document(), source_key="test:plan")
    await store.close()
    store = await Store.open(path)
    try:
        memory = ResearchMemory(store)
        for query in ("Pt111 reaction", "Pt(111) 反应", "Cu(100) adsorption"):
            found = await memory.search(second, MemorySearch(query=query))
            assert found["items"][0]["id"] == item["id"]
            assert found["items"][0]["matched_terms"]
        assert not (await memory.search(second, MemorySearch(query="Mg hydration")))["items"]
        restored = await memory.read(second, item["id"])
        assert restored["parameters"]["incar"]["ENCUT"] == 520
        assert restored["evidence"]["level"] == "unverified"
    finally:
        await store.close()


async def test_project_scoping_is_dynamic_personal_scope_explicit_and_no_other_owner(tmp_path):
    store = await Store.open(tmp_path / "scope.sqlite3")
    try:
        a = (await store.create_session("a"))["id"]
        b = (await store.create_session("b"))["id"]
        project = str(uuid4())
        async with store.transaction():
            await store.db.execute("INSERT INTO projects VALUES(?,NULL,'project')", (project,))
            await store.db.execute("INSERT INTO project_sessions VALUES(?,?)", (a, project))
        memory = ResearchMemory(store)
        await memory.save(a, document(), source_key="plan:1")
        assert not (await memory.search(b, MemorySearch(query="Pt111")))["items"]
        assert (await memory.search(b, MemorySearch(query="Pt111", scope="personal")))["items"]
        async with store.transaction():
            await store.db.execute("INSERT INTO project_sessions VALUES(?,?)", (b, project))
        assert (await memory.search(b, MemorySearch(query="Pt111")))["items"]
    finally:
        await store.close()


async def test_requested_facet_and_plan_rank_before_short_wrong_facet_hits(tmp_path):
    store = await Store.open(tmp_path / "ranking.sqlite3")
    try:
        session = (await store.create_session("ranking"))["id"]
        memory = ResearchMemory(store)
        for index in range(30):
            await memory.save(
                session,
                document(
                    title=f"Cu(100) adsorption {index}",
                    content="吸附能 adsorption",
                    systems=["Cu(100)"],
                ),
                source_key=f"wrong:{index}",
            )
        target = await memory.save(
            session,
            document(
                title="Cu(111) reaction scheme",
                content="长流程说明 " * 1000,
                systems=["Cu(111)"],
                tasks=["reaction"],
            ),
            source_key="target",
        )
        await memory.save(
            session,
            document(
                title="Cu111 adsorption gas model",
                kind="calculation",
                content="adsorption",
            ),
            source_key="short-node",
        )
        hits = await memory.search(session, MemorySearch(query="Cu(111) 吸附能", limit=3))
        assert hits["items"][0]["id"] == target["id"]
        assert hits["items"][0]["matched_facets"] == ["cu111"]
        assert hits["items"][0]["kind"] == "plan"
        assert hits["truncated"]
    finally:
        await store.close()


def test_selected_conversation_scope_differs_from_shared_project_and_paginates(settings_factory):
    with TestClient(
        create_app(settings_factory(), transport=httpx.MockTransport(response))
    ) as client:
        first = client.post("/sessions", json={"title": "Pt111 adsorption"}).json()["id"]
        second = client.post("/sessions", json={"title": "Pt111 reaction"}).json()["id"]
        first_path = f"/api/sessions/{first}/memories"
        second_path = f"/api/sessions/{second}/memories"
        first_ids = set()
        for index in range(23):
            saved = client.post(first_path, json=document(title=f"Pt111 {index}").model_dump())
            first_ids.add(saved.json()["id"])
        other = client.post(second_path, json=document(title="Pt111 reaction").model_dump()).json()
        page_one = client.get(first_path, params={"scope": "session", "query": "Pt111"}).json()
        page_two = client.get(
            first_path, params={"scope": "session", "query": "Pt111", "offset": 20}
        ).json()
        assert page_one["truncated"] and not page_two["truncated"]
        hits = page_one["items"] + page_two["items"]
        assert len(hits) == 23 and {item["id"] for item in hits} == first_ids
        assert all(item["session_id"] == first for item in hits)
        assert all(item["session_title"] == "Pt111 adsorption" for item in hits)
        selected = client.get(second_path, params={"scope": "session"}).json()["items"]
        assert [item["id"] for item in selected] == [other["id"]]
        # Ungrouped conversations intentionally share the project/personal area.
        for scope in ("project", "personal"):
            a = client.get(first_path, params={"scope": scope}).json()
            b = client.get(second_path, params={"scope": scope}).json()
            assert a == b
        assert (
            client.get(first_path, params={"scope": "session", "query": "no_match"}).json()["items"]
            == []
        )
        assert client.get(first_path, params={"offset": -1}).status_code == 422


async def test_versions_concurrent_updates_and_forgotten_source_stays_forgotten(tmp_path):
    store = await Store.open(tmp_path / "version.sqlite3")
    try:
        session = (await store.create_session("a"))["id"]
        memory = ResearchMemory(store)
        item = await memory.save(session, document(), source_key="plan:1")
        again = await memory.save(session, document(), source_key="plan:1")
        assert again["version"] == 1 and again["unchanged"]
        updated = await memory.save(
            session, document(content="修正参照态"), source_key="plan:1", expected_version=1
        )
        assert updated["version"] == 2
        with pytest.raises(AgentError, match="记忆已更新"):
            await memory.save(session, document(), source_key="plan:1", expected_version=1)
        with pytest.raises(AgentError):
            await memory.forget(session, item["id"], 1)
        await memory.forget(session, item["id"], 2)
        suppressed = await memory.save(session, document(content="再次同步"), source_key="plan:1")
        assert suppressed["forgotten"]
        assert not (await memory.search(session, MemorySearch()))["items"]
        with pytest.raises(AgentError):
            await memory.read(session, item["id"])
        row = await store._one("SELECT COUNT(*) n FROM research_memory_versions")
        assert row["n"] == 2
    finally:
        await store.close()


def test_parameter_diff_missing_case_mesh_and_convergence_are_not_scientific_validation():
    result = parameter_diff(
        {"incar": {"encut": 520, "ISPIN": 2}, "kmesh": [4, 4, 1]},
        {"incar": {"ENCUT": 520}, "kmesh": [3, 3, 1]},
    )
    assert {row["parameter"] for row in result} == {"incar.ISPIN", "kmesh"}
    assert result[0]["current_present"] is False
    job = {"finished_ok": True, "results": {"run_status": {"electronic_converged": True}}}
    assert calculation_evidence(job, {})["level"] == "converged"
    assert calculation_evidence(job, {"require_ionic": True})["level"] == "observed"
    assert calculation_evidence({"finished_ok": True}, {})["level"] == "observed"
    assert calculation_evidence(job, {})["scientifically_validated"] is False


def test_large_calculation_document_is_explicitly_excerpted_not_silently_lost():
    item = capture_document(title="大型流程", kind="plan", content="计算方案\n" * 50000)
    assert item.content.startswith("[大型源记录摘录")
    assert "plan_id/node_id" in item.content
    assert "后文未进入记忆索引" in item.content
    assert len(item.content.encode()) < 160000


async def test_legacy_job_retry_capture_and_diagnostic_parameter_comparison(tmp_path):
    store = await Store.open(tmp_path / "legacy.sqlite3")
    try:
        session = (await store.create_session("a"))["id"]
        artifact = {
            "preview_id": "preview-new",
            "structure_id": "structure-a",
            "request": {"incar": {"NSW": 0, "NELM": 120}, "kmesh": [4, 4, 1]},
            "recovery_source_job_id": "failed-job",
            "recovery_source_preview_id": "preview-old",
            "recovery_source_error": "SCF failed",
            "recovery_reason": "增加电子步数",
            "recovery_before_parameters": {"incar": {"NSW": 0, "NELM": 60}},
        }
        job = {
            "id": "success",
            "label": "Pt111",
            "process_state": "finished",
            "finished_ok": True,
            "results": {"run_status": {"electronic_converged": True}},
        }
        await capture_job(store, session, artifact, job)
        memory = ResearchMemory(store)
        lessons = await memory.search(session, MemorySearch(kind="lesson"))
        assert len(lessons["items"]) == 1
        assert lessons["items"][0]["evidence"]["level"] == "converged"
        current = {"id": "current-failed", "label": "Pt111", "exit_message": "SCF failed"}
        references = await diagnostic_references(
            store,
            session,
            current,
            {
                "request": {"incar": {"NELM": 60}},
                "structure": {"formula": "Pt111"},
            },
        )
        assert references["items"]
        differences = references["items"][0]["differences"]
        assert any(
            row["parameter"] == "incar.NELM" and row["reference"] == 120 and row["current"] == 60
            for row in differences
        )
        store.research_memory_enabled = False
        assert not (await diagnostic_references(store, session, current, artifact))["items"]
    finally:
        await store.close()


def plan_fixture():
    node = {
        "id": "static",
        "title": "Pt(111) 静态",
        "kind": "vasp",
        "calculation": {"require_electronic": True, "require_ionic": False},
        "effective": {"incar": {"ENCUT": 520, "NELM": 60}, "kmesh": [4, 4, 1]},
    }
    return {
        "id": str(uuid4()),
        "revision": 1,
        "title": "Pt(111) 吸附能",
        "goal": "比较吸附位点",
        "status": "failed",
        "nodes": [node],
        "research_systems": ["Pt(111)", "CO"],
        "research_tasks": ["吸附能"],
        "runtime": {"static": {"status": "failed", "error": "电子不收敛", "job_id": "failed-job"}},
    }


async def test_capture_repair_chain_preserves_each_failed_retry_and_provenance(tmp_path):
    store = await Store.open(tmp_path / "capture.sqlite3")
    try:
        session = (await store.create_session("计算"))["id"]
        memory = ResearchMemory(store)
        plan = plan_fixture()
        await capture_plan(memory, session, plan)
        first = copy.deepcopy(plan["nodes"][0])
        failed = copy.deepcopy(plan["runtime"]["static"])
        second = copy.deepcopy(first)
        second["effective"]["incar"]["NELM"] = 120
        plan["nodes"][0]["effective"]["incar"].update(NELM=120, ALGO="Normal")
        plan["runtime"]["static"] = {
            "status": "completed",
            "input_structure_id": "structure-123",
            "job": {
                "id": "success-job",
                "pk": 111,
                "finished_ok": True,
                "results": {"run_status": {"electronic_converged": True}},
            },
            "attempts": [
                {"node": first, "state": failed, "reason": "增加 NELM"},
                {"node": second, "state": failed, "reason": "切换 ALGO"},
            ],
        }
        plan["status"] = "completed"
        await capture_plan(memory, session, plan)
        await capture_plan(memory, session, plan)
        lessons = (await memory.search(session, MemorySearch(kind="lesson")))["items"]
        assert len(lessons) == 2
        entries = [await memory.read(session, item["id"]) for item in lessons]
        early = next(item for item in entries if item["evidence"]["attempt"] == 1)
        late = next(item for item in entries if item["evidence"]["attempt"] == 2)
        assert early["evidence"]["level"] == "observed"
        assert json.loads(early["content"])["retry_outcome"] == "failed"
        assert late["evidence"]["level"] == "converged"
        assert late["evidence"]["job_id"] == "success-job"
        differences = json.loads(late["content"])["parameter_changes"]
        assert [row["parameter"] for row in differences] == ["incar.ALGO"]
        row = await store._one("SELECT COUNT(*) n FROM research_memories")
        assert row["n"] == 4  # plan, node, two retry lessons
    finally:
        await store.close()


def test_api_export_revision_read_update_forget_and_no_html_execution(settings_factory):
    with TestClient(
        create_app(settings_factory(), transport=httpx.MockTransport(response))
    ) as client:
        session = client.post("/sessions", json={"title": "记忆"}).json()["id"]
        path = f"/api/sessions/{session}/memories"
        body = document(content="<script>alert('untrusted')</script>").model_dump()
        saved = client.post(path, json=body)
        assert saved.status_code == 200, saved.text
        item = saved.json()
        assert client.post(path, json=body).json()["id"] == item["id"]
        full = client.get(f"{path}/{item['id']}").json()
        assert full["evidence"]["level"] == "unverified"
        exported = client.get(f"{path}/{item['id']}/document.md")
        assert "520" in exported.text and "来源、适用范围" in exported.text
        assert exported.headers["content-type"].startswith("text/markdown")
        forged = client.post(path, json=body | {"evidence": {"level": "converged"}})
        assert forged.status_code == 422
        updated = client.post(
            path,
            json=body | {"memory_id": item["id"], "expected_version": 1, "content": "修正参照态"},
        )
        assert updated.json()["version"] == 2
        assert client.get(f"{path}/{item['id']}/versions/1").json()["content"] == body["content"]
        assert len(client.get(f"{path}/{item['id']}/versions").json()) == 2
        assert client.get("/memory").status_code == 200
        # Rendered documents use the shared HTML-disabled Markdown renderer;
        # frontend tests exercise malicious titles, JSON fields and Markdown.
        assert client.get("/assets/memory-view.mjs").status_code == 200
        assert (
            client.post(f"{path}/{item['id']}/forget", json={"expected_version": 2}).status_code
            == 204
        )
        assert not client.get(path).json()["items"]
        assert client.get(f"{path}/{item['id']}/versions/1").status_code == 404


async def test_account_isolation_all_routes_and_model_tools(settings_factory):
    settings = settings_factory(auth_enabled=True, cookie_secure=False)
    password = "a strong test passphrase"
    await create_user_account(settings, "alice", password)
    await create_user_account(settings, "bob", password)
    app = create_app(settings, transport=httpx.MockTransport(response))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as alice,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as bob,
    ):
        for name, client in (("alice", alice), ("bob", bob)):
            assert (
                await client.post("/auth/login", json={"username": name, "password": password})
            ).status_code == 200
        a = (await alice.post("/sessions", json={"title": "a"})).json()["id"]
        b = (await bob.post("/sessions", json={"title": "b"})).json()["id"]
        path = f"/api/sessions/{a}/memories"
        item = (await alice.post(path, json=document().model_dump())).json()
        # The session-free platform API must enforce the same authenticated owner.
        platform_path = f"/api/research/memories/{item['id']}"
        assert (await alice.get(platform_path)).status_code == 200
        for suffix in ("", "/document.md", "/versions", "/versions/1"):
            assert (await bob.get(platform_path + suffix)).status_code == 404
        assert not (await bob.get("/api/research/memories?scope=personal")).json()["items"]
        assert (
            await bob.post(platform_path + "/forget", json={"expected_version": 1})
        ).status_code == 404
        assert (
            await bob.post(
                "/api/research/memories",
                json=document().model_dump()
                | {
                    "memory_id": item["id"],
                    "expected_version": 1,
                },
            )
        ).status_code == 404
        project = (await alice.post("/api/projects", json={"name": "private"})).json()["id"]
        assert (await bob.get(f"/api/research/memories?project_id={project}")).status_code == 404
        assert (await bob.get(path)).status_code == 404
        assert (await bob.get(path + "?scope=session")).status_code == 404
        other = f"/api/sessions/{b}/memories/{item['id']}"
        for suffix in ("", "/document.md", "/versions", "/versions/1"):
            assert (await bob.get(other + suffix)).status_code == 404
        assert (await bob.post(other + "/forget", json={"expected_version": 1})).status_code == 404
        assert not (await bob.get(f"/api/sessions/{b}/memories?scope=personal")).json()["items"]
        with pytest.raises(AgentError):
            await ResearchMemory(app.state.store).read(b, item["id"])


def test_automatic_recall_is_bounded_reference_data_and_disabled_setting(settings_factory):
    requests = []
    settings = settings_factory()
    counter = TokenCounter(settings.tokenizer_path)

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert (
            counter.request_tokens(payload["messages"], payload.get("tools", []))
            <= settings.context_input_budget
        )
        return response(request)

    with TestClient(create_app(settings, transport=httpx.MockTransport(provider))) as client:
        a = client.post("/sessions", json={"title": "a"}).json()["id"]
        item = client.post(f"/api/sessions/{a}/memories", json=document().model_dump()).json()
        result = client.post("/chat", json={"message": "做 Pt(111) 反应能计算"})
        assert result.status_code == 200, result.text
        recalled = [
            m for m in requests[-1]["messages"] if "<research_memory>" in str(m.get("content"))
        ]
        assert len(recalled) == 1 and recalled[0]["role"] == "user"
        assert item["id"] in recalled[0]["content"]
        history = client.get(f"/sessions/{result.json()['session_id']}").json()["messages"]
        assert "<research_memory>" not in json.dumps(history)
    disabled = settings_factory(research_memory_enabled=False)
    with TestClient(create_app(disabled, transport=httpx.MockTransport(response))) as client:
        names = {tool["function"]["name"] for tool in client.get("/tools").json()}
        assert "memory_save" not in names
        a = client.post("/sessions", json={"title": "a"}).json()["id"]
        assert client.get(f"/api/sessions/{a}/memories").status_code == 404
