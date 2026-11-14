"""Compact notifications retain full evidence and real continuation/recovery behavior."""

import copy
import json
from uuid import uuid4

import httpx
import pytest
from test_slurm_jobs import LocalSlurm, job, tick
from test_slurm_jobs import cluster as cluster_fixture

from chemistry_agent.api import create_app
from chemistry_agent.automation.job_events import wake
from chemistry_agent.automation.plan_authority import auto_queue
from chemistry_agent.automation.plan_models import PlanDraft
from chemistry_agent.automation.wait_models import WaitRegister
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.event_context import build_wakeup_message
from chemistry_agent.tools import ToolContext

cluster = cluster_fixture


def test_large_scientific_data_stays_out_of_notification() -> None:
    plan = {
        "id": str(uuid4()),
        "revision": 4,
        "approved_revision": 3,
        "status": "running",
        "goal": "large goal" * 10000,
        "nodes": [
            {
                "id": "relax",
                "kind": "task",
                "title": "Relax",
                "purpose": "old purpose",
                "text_files": {"INCAR": "large input" * 10000},
            },
            {"id": "later", "kind": "task", "purpose": "unrelated" * 10000},
        ],
        "runtime": {"relax": {"status": "completed"}},
    }
    payload = {
        "instruction": "follow-up" * 1000,
        "events": [
            {
                "node_id": "relax",
                "type": "step_ready",
                "outcome": "pending",
                "attempt": "0",
                "instruction": "old purpose",
                "state": {
                    "outputs": {"result": "large output" * 10000},
                    "instruction": "job instruction" * 1000,
                    "error": "full diagnostic" * 1000,
                },
            }
        ],
        "file_observations": {"relax": {"attempt": "0", "files": ["a.dat"] * 10000}},
    }
    originals = copy.deepcopy((plan, payload))
    message = build_wakeup_message(plan, "event-1", payload)
    body = json.loads(message.split("\n", 1)[1])
    assert len(message) < 3000
    assert "large input" not in message and "large output" not in message
    assert "old purpose" not in message and "unrelated" not in message
    assert "research_steps" not in body and "goal" not in body
    assert body["plan_revision"] == 4 and body["approved_revision"] == 3
    assert body["events"][0]["current_status"] == "completed"
    assert body["instruction"]["read_from"] == "payload.instruction"
    assert body["events"][0]["instruction"]["read_from"].endswith("state.instruction")
    assert body["file_observations"][0]["file_count"] == 10000
    assert (plan, payload) == originals


def test_bundled_outcomes_and_job_identifiers_are_preserved() -> None:
    plan = {
        "id": "plan",
        "status": "running",
        "nodes": [
            {"id": key, "kind": "job", "parent_task": "screen", "job": {"host": "test"}}
            for key in ("a", "b")
        ],
        "runtime": {"a": {"status": "completed"}, "b": {"status": "failed"}},
    }
    payload = {
        "instruction": "Inspect both outcomes",
        "events": [
            {
                "node_id": key,
                "outcome": state["status"],
                "attempt": key,
                "state": {
                    "generic_job_id": f"job-{key}",
                    "scheduler_job_id": key,
                    "remote_directory": f"/work/{key}",
                },
            }
            for key, state in plan["runtime"].items()
        ],
    }
    body = json.loads(build_wakeup_message(plan, "event", payload).split("\n", 1)[1])
    assert body["instruction"] == payload["instruction"]
    assert [event["outcome"] for event in body["events"]] == ["completed", "failed"]
    assert [event["job_id"] for event in body["events"]] == ["job-a", "job-b"]
    assert all(event["parent_task"] == "screen" for event in body["events"])


async def test_agent_reads_evidence_continues_and_retries_without_resubmission(cluster) -> None:
    original, _, tmp = cluster
    phase = 0
    event_body = {}
    long_instruction = "Check output, then reuse it in the next job. " * 80

    def tool_call(name: str, arguments: dict) -> dict:
        return {
            "id": f"{phase}-{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def completion(request: httpx.Request) -> httpx.Response:
        nonlocal phase, event_body
        phase += 1
        data = json.loads(request.content)
        names = {tool["function"]["name"] for tool in data["tools"]}
        assert {
            "calculation_job_files",
            "calculation_plan_get",
            "calculation_plan_extend",
            "remote_shell",
            "calculation_wait_control",
        } <= names
        content = {"role": "assistant"}
        finish = "tool_calls"
        if phase == 1:
            message = next(
                m["content"]
                for m in data["messages"]
                if m.get("content", "").startswith("后台任务结束事件")
            )
            event_body = json.loads(message.split("\n", 1)[1])
            assert len(message) < 3000 and "research_steps" not in event_body
            assert event_body["instruction"]["read_from"] == "payload.instruction"
            content["tool_calls"] = [
                tool_call(
                    "calculation_plan_get", {"plan_id": event_body["plan_id"], "node_id": "compute"}
                ),
                tool_call("calculation_wait_control", event_body["event_details"]["arguments"]),
            ]
        elif phase == 2:
            results = [json.loads(m["content"]) for m in data["messages"][-2:]]
            assert results[0]["node"]["job"]["files"]["INCAR"] == "ENCUT = 450"
            assert "payload" in results[1], results[1]
            assert results[1]["payload"]["instruction"] == long_instruction
            content["tool_calls"] = [
                tool_call(
                    "calculation_job_files",
                    {
                        "job_id": event_body["events"][0]["job_id"],
                        "mode": "read",
                        "path": "result.dat",
                    },
                )
            ]
        elif phase == 3:
            assert json.loads(data["messages"][-1]["content"])["content"] == "2"
            next_job = job(tmp, "next", "cat upstream.dat > result.dat")
            next_job["files"] = [
                {"node": "compute", "path": "result.dat", "target": "upstream.dat"}
            ]
            content["tool_calls"] = [
                tool_call(
                    "calculation_plan_extend",
                    {
                        "plan_id": event_body["plan_id"],
                        "reason": "Use inspected output",
                        "nodes": [next_job],
                    },
                )
            ]
        elif phase == 4:
            return httpx.Response(429)
        else:
            assert phase == 5
            assert json.loads(data["messages"][-1]["content"])["added_nodes"] == ["next"]
            content["content"], finish = "已读取原始结果并登记下一步。", "stop"
        return httpx.Response(
            200, json={"choices": [{"message": content, "finish_reason": finish}]}
        )

    app = create_app(original.chat.service.settings, transport=httpx.MockTransport(completion))
    async with app.router.lifespan_context(app):
        plans = app.state.calculation_chat.plans
        await plans.close()
        plans.slurm = LocalSlurm(plans.chat)
        session = (await plans.store.create_session("compact wakeup"))["id"]
        context = ToolContext(session, str(uuid4()))
        token = auto_queue.set("low")
        try:
            plan = await plans.draft(
                PlanDraft(title="two jobs", goal="continue", nodes=[job(tmp)]), context
            )
        finally:
            auto_queue.reset(token)
        identifier = plan["id"]
        await plans.platform.waits.register(
            WaitRegister(
                plan_id=identifier,
                key="finish-first",
                instruction=long_instruction,
                condition={"op": "state", "node": "compute", "states": ["completed"]},
            ),
            context,
        )
        agent, plans.agent = plans.agent, None
        await tick(plans, context, identifier)
        await tick(plans, context, identifier)
        # The event snapshot predates this saved input; on-demand reads must see the edit.
        current = plans.chat.load(session, "plans", identifier)
        current["nodes"][0]["job"]["files"] = {"INCAR": "ENCUT = 450"}
        plans.save(session, current)
        plans.agent = agent
        with pytest.raises(AgentError) as captured:
            await wake(plans, session, identifier)
        assert captured.value.code == "provider_rate_limited", repr(captured.value.__cause__)
        await wake(plans, session, identifier)
        assert phase == 4
        async with plans.store.transaction():
            await plans.store.db.execute(
                "UPDATE agent_events SET available_at=0 WHERE status='retry_wait'"
            )
        await wake(plans, session, identifier)
        await wake(plans, session, identifier)
        assert phase == 5
        current = plans.chat.load(session, "plans", identifier)
        assert [node["id"] for node in current["nodes"]].count("next") == 1
        # Full event reads are session AND plan scoped, with no delivery side effects.
        arguments = event_body["event_details"]["arguments"]
        other = (await plans.store.create_session("other"))["id"]
        plans.chat.save(other, "plans", identifier, current)
        with pytest.raises(AgentError, match="不存在此事件"):
            await plans.platform.execute("wait.control", arguments, ToolContext(other, "read"))
        another = copy.deepcopy(current)
        another["id"] = str(uuid4())
        plans.save(session, another)
        with pytest.raises(AgentError, match="不存在此事件"):
            await plans.platform.execute(
                "wait.control", arguments | {"plan_id": another["id"]}, context
            )
        details = await plans.platform.execute("wait.control", arguments, context)
        assert details["status"] == "completed"
        assert details["payload"]["instruction"] == long_instruction
        plans.agent = None
        await tick(plans, context, identifier)
        final = await tick(plans, context, identifier)
        assert final["status"] == "completed"
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
        assert "后台任务结束事件" not in json.dumps(
            await plans.store.get_session(session), ensure_ascii=False
        )
