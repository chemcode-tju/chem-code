"""User edits must reach actual inputs while preserving submitted attempts."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from test_slurm_jobs import cluster as cluster_fixture
from test_slurm_jobs import job, tick

from chemistry_agent.api import create_app
from chemistry_agent.automation.generic_step import inspect_generic_step
from chemistry_agent.automation.plan_models import PlanDraft, PlanQuery
from chemistry_agent.automation.step_edit import StepEdit, edit_step
from chemistry_agent.errors import AgentError

cluster = cluster_fixture


async def save(plans, context, plan, field, content, node="compute"):
    return await edit_step(
        plans,
        context.session_id,
        plan["id"],
        node,
        StepEdit(
            expected_revision=plan["revision"],
            field=field,
            content=content,
        ),
    )


async def test_edited_files_and_script_are_executed(cluster):
    plans, context, tmp = cluster
    plan = await plans.draft(PlanDraft(title="edit", goal="test", nodes=[job(tmp)]), context)
    plan = await save(plans, context, plan, "INCAR", "ENCUT = 450\nEDIFF = 1E-6\n")
    plan = await save(plans, context, plan, "KPOINTS", "Gamma\n0\nGamma\n2 2 1\n0 0 0\n")
    plan = await save(plans, context, plan, "script", "cat INCAR KPOINTS > result.dat\n")
    assert not plans.chat.list_records(context.session_id, "slurm_jobs")
    previous = plans.chat.list_records(context.session_id, "plan_versions")
    assert len(previous) == 3 and previous[0]["revision"] != plan["revision"]
    await plans.confirm(context.session_id, plan["id"], plan["revision"])
    await tick(plans, context, plan["id"])
    final = await tick(plans, context, plan["id"])
    assert final["runtime"]["compute"]["status"] == "completed"
    receipt = plans.slurm.load(context.session_id, final["runtime"]["compute"]["generic_job_id"])
    from pathlib import Path

    result = (Path(receipt["retrieved_directory"]) / "result.dat").read_text()
    assert "ENCUT = 450" in result and "2 2 1" in result
    with pytest.raises(AgentError, match="锁定"):
        await save(plans, context, final, "script", "echo replaced")


async def test_unstarted_task_edit_is_visible_to_agent_and_versioned(cluster):
    plans, context, tmp = cluster
    plan = await plans.draft(
        PlanDraft(
            title="goal",
            goal="test",
            nodes=[
                {"id": "surface", "kind": "task", "title": "表面", "purpose": "原说明"},
            ],
        ),
        context,
    )
    # Editing an unstarted step is also supported after the overall plan is confirmed.
    plan = await plans.confirm(context.session_id, plan["id"], plan["revision"])
    original = plan
    plan = await save(plans, context, plan, "purpose", "固定底部两层，真空 15 Å。", "surface")
    plan = await save(plans, context, plan, "INCAR", "ENCUT = 450\n", "surface")
    plan = await save(plans, context, plan, "script", "#!/bin/bash\nvasp_std\n", "surface")
    queried = await plans.query(PlanQuery(plan_id=plan["id"], node_id="surface"), context)
    assert queried["node"]["purpose"] == "固定底部两层，真空 15 Å。"
    assert queried["node"]["text_files"]["INCAR"] == "ENCUT = 450\n"
    assert "vasp_std" in queried["node"]["submission_script"]
    info = await inspect_generic_step(
        plans.chat, context.session_id, plan, plan["nodes"][0], "info", None
    )
    assert info["editing"]["files"] and info["parameters"]["INCAR"] == "ENCUT = 450\n"
    with pytest.raises(AgentError, match="已有更新"):
        await save(plans, context, original, "purpose", "过期覆盖", "surface")


@pytest.mark.parametrize(
    "state",
    [
        {"status": "preparing"},
        {"status": "running"},
        {"status": "completed"},
        {"status": "failed"},
        {"status": "pending", "generic_job_id": "submitted"},
        {"status": "pending", "jobs": ["queued"]},
    ],
)
async def test_state_rechecked_on_save(cluster, state):
    plans, context, tmp = cluster
    plan = await plans.draft(PlanDraft(title="edit", goal="test", nodes=[job(tmp)]), context)
    plan["runtime"]["compute"] = state
    plans.save(context.session_id, plan)
    for field in ("INCAR", "KPOINTS", "script", "purpose"):
        with pytest.raises(AgentError, match="锁定"):
            await save(plans, context, plan, field, "new")
    assert not plans.chat.list_records(context.session_id, "plan_versions")


async def test_concurrent_edits_have_one_winner(cluster):
    plans, context, tmp = cluster
    plan = await plans.draft(PlanDraft(title="edit", goal="test", nodes=[job(tmp)]), context)
    results = await asyncio.gather(
        save(plans, context, plan, "INCAR", "ENCUT = 450"),
        save(plans, context, plan, "INCAR", "ENCUT = 520"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, AgentError) for item in results) == 1
    assert len(plans.chat.list_records(context.session_id, "plan_versions")) == 1


def test_http_editor_persists_and_rejects_a_claimed_task(settings_factory):
    settings = settings_factory(
        automation_enabled=True, platform_agent_enabled=False, deepseek_api_key=None
    )
    app = create_app(
        settings, transport=httpx.MockTransport(lambda request: pytest.fail("No model call"))
    )
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as client:
        session = client.post("/sessions", json={"title": "editor acceptance"}).json()["id"]
        base = f"/api/automation/chat/{session}/workflows"
        response = client.post(
            base,
            json={
                "title": "editor",
                "goal": "test",
                "nodes": [
                    {"id": "surface", "title": "surface", "kind": "task"},
                ],
            },
        )
        assert response.status_code == 200, response.text
        plan = response.json()
        path = f"{base}/{plan['id']}/steps/surface"
        response = client.post(
            path + "/edit",
            json={
                "expected_revision": 1,
                "field": "INCAR",
                "content": "ENCUT = 450\n",
            },
        )
        assert response.status_code == 200, response.text
        assert client.get(path + "/info").json()["parameters"]["INCAR"] == "ENCUT = 450\n"
        assert (
            client.post(
                path + "/edit",
                json={
                    "expected_revision": 1,
                    "field": "purpose",
                    "content": "stale",
                },
            ).status_code
            == 409
        )
        client.post(f"{base}/{plan['id']}/confirm", json={"revision": 2}).raise_for_status()
        client.portal.call(app.state.calculation_chat.plans.tick_plan, session, plan["id"])

        async def claim_event():
            async with app.state.store.transaction():
                await app.state.store.db.execute(
                    "UPDATE agent_events SET status='running' WHERE session_id=?",
                    (session,),
                )

        client.portal.call(claim_event)
        response = client.post(
            path + "/edit",
            json={
                "expected_revision": 2,
                "field": "purpose",
                "content": "too late",
            },
        )
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "step_edit_locked"
        )
