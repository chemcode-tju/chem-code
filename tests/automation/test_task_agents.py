"""Two real platform adapters against simulated native protocols and Slurm."""

import asyncio
import json
import subprocess
from uuid import UUID, uuid4

import httpx
import pytest
from test_slurm_jobs import LocalSlurm, draft, job, tick
from test_slurm_jobs import cluster as cluster_fixture

from chemistry_agent.api import create_app
from chemistry_agent.automation import slurm_remote
from chemistry_agent.automation.job_events import wake
from chemistry_agent.backends.antigravity import AntigravityBackend
from chemistry_agent.backends.codex import CodexBackend
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.steps import StepReport
from chemistry_agent.platform.task_agents import (
    TaskAgentScope,
    TaskSubmission,
    current_task_agent,
    submit_task_job,
    task_session,
)
from chemistry_agent.tools import ToolContext

cluster = cluster_fixture


@pytest.mark.asyncio
async def test_attention_reconciles_only_and_holds_unconverged_neb(cluster, monkeypatch):
    fixture, _, tmp = cluster
    app = create_app(fixture.chat.service.settings)
    async with app.router.lifespan_context(app):
        plans = app.state.calculation_chat.plans
        await plans.close()
        plans.slurm = LocalSlurm(plans.chat)
        request = plans.slurm.request
        run = slurm_remote.run
        calls = []

        def lose_reply(arguments: list[str]) -> subprocess.CompletedProcess:
            result = run(arguments)
            if arguments[0] == "sbatch":
                raise subprocess.TimeoutExpired("sbatch", 25)
            return result

        async def request_with_timeout(host: str, payload: dict) -> dict:
            calls.append(payload["action"])
            try:
                return await request(host, payload)
            except subprocess.TimeoutExpired as exc:
                raise AgentError("timeout", "simulated lost response", 503) from exc

        async def skip_dispatch(*_) -> None:
            pass

        monkeypatch.setattr(slurm_remote, "run", lose_reply)
        monkeypatch.setattr(plans.slurm, "request", request_with_timeout)
        monkeypatch.setattr(plans.dispatcher, "offer", skip_dispatch)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            context = ToolContext(session, "fixture")
            neb_script = (
                "mkdir -p paths/step_7\n"
                "cp path.json paths/step_7/path.json\n"
                "for index in 00 01 02; do "
                "printf 'test snapshot\n' > paths/step_7/image_${index}.vasp; done\n"
                "printf 2 > result.dat"
            )
            scientific_job = job(
                tmp, "path", neb_script, results="neb", neb_force_target=0.05,
                files={"path.json": json.dumps({
                    "step": 7, "energy_TOTEN_eV": [-1, 0, -0.8],
                    "projected_fmax_eV_A": 0.082, "all_scf_valid": True,
                })},
            )["job"]
            dependant = job(tmp, "compare") | {"depends_on": ["neb"]}
            identifier = await draft(plans, context, [
                {"id": "neb", "kind": "task", "title": "Cu CI-NEB"},
                dependant,
            ])
            task_id = task_session(session, identifier, "neb")
            scope = TaskAgentScope(session, identifier, "neb", task_id)
            token = current_task_agent.set(scope)
            try:
                submitted = await submit_task_job(
                    plans, TaskSubmission(key="neb", title="模拟路径", job=scientific_job), context
                )
            finally:
                current_task_agent.reset(token)
            node_id = submitted["node_id"]
            plan = await tick(plans, context, identifier)
            attempt = plan["runtime"][node_id]["generic_job_id"]
            base = f"/api/automation/chat/{session}/workflows/{identifier}"
            (tmp / "accounting_down").touch()
            items = (await client.get(f"{base}/attention")).json()["items"]
            assert [(item["kind"], item["node_id"], item["job_id"]) for item in items] == [
                ("submission", "neb", attempt)
            ]
            assert (
                await client.post(f"{base}/steps/compare/jobs/{attempt}/reconcile")
            ).status_code == 404
            reconcile = f"{base}/steps/neb/jobs/{attempt}/reconcile"
            for _ in range(2):
                data = (await client.post(reconcile)).json()
                assert data["submission_status"] == "uncertain"
                assert data["scheduler_job_id"] is None
            await tick(plans, context, identifier)
            assert calls.count("submit") == 1
            (tmp / "accounting_down").unlink()
            data = (await client.post(reconcile)).json()
            assert data["scheduler_job_id"] == "100"
            assert (await client.get(f"{base}/attention")).json()["items"] == []

            plan = await tick(plans, context, identifier)
            path = plan["runtime"][node_id]
            assert path["job"]["scheduler_state"] == "completed"
            assert path["status"] == "failed"
            assert path["error_code"] == "neb_unconverged"
            assert plan["runtime"]["compare"]["status"] == "pending"
            assert "generic_job_id" not in plan["runtime"]["compare"]
            items = (await client.get(f"{base}/attention")).json()["items"]
            assert [(item["kind"], item["node_id"], item["job_id"]) for item in items] == [
                ("science", "neb", attempt)
            ]
            assert items[0]["science"] == {
                "kind": "neb", "status": "unconverged", "target": 0.05, "force": 0.082,
                "step": 7, "source": "paths/step_7/path.json",
            }
            with pytest.raises(AgentError) as rejected:
                await plans.platform.report(StepReport(
                    plan_id=UUID(identifier), node_id="neb", expected_revision=plan["revision"],
                    status="completed", reason="attempt to adopt a nonconverged path",
                    accepted_jobs=[UUID(attempt)],
                ), context)
            assert rejected.value.code == "neb_acceptance_required"
            assert calls.count("submit") == 1
            assert list(json.loads((tmp / "scheduler.json").read_text())) == ["100"]


@pytest.mark.asyncio
async def test_task_agent_threads_restore_without_resubmitting_or_leaking(cluster, monkeypatch):
    original, _, tmp = cluster
    settings = original.chat.service.settings
    private_logs = {"codex": "CODEX-PRIVATE-LOG", "antigravity": "AGY-PRIVATE-LOG"}
    native_ids = {backend: str(uuid4()) for backend in private_logs}
    deliveries = []
    plan_id = None
    phase = "initial"
    main_inputs = []
    submissions = {backend: job(tmp, backend)["job"] for backend in private_logs}

    def model_reply(request):
        main_inputs.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "主对话仍可使用"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    def command(backend, progress):
        if not progress:
            return "calculation_task_submit", {
                "key": "initial",
                "title": "模拟计算",
                "job": submissions[backend],
            }
        node = plan["runtime"][backend]
        return "calculation_step_report", {
            "plan_id": plan_id,
            "node_id": backend,
            "expected_revision": plan["revision"],
            "status": "completed",
            "reason": "核对模拟输出",
            "accepted_jobs": node["jobs"],
            "result": {"energy_ev": 0.2, "converged": True},
        }

    class CodexRPC:
        def __init__(self, _):
            self.events = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def request(self, method, params):
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            if method == "model/list":
                return {
                    "data": [
                        {
                            "model": "codex-test",
                            "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                        }
                    ]
                }
            if method in {"thread/start", "thread/resume"}:
                assert params["model"] == "codex-test"
                assert params["config"]["model_reasoning_effort"] == "high"
                if method == "thread/start":
                    assert "calculation_task_submit" in {
                        entry["name"] for entry in params["dynamicTools"]
                    }
                    assert "remote_shell" not in {entry["name"] for entry in params["dynamicTools"]}
                else:
                    assert params["threadId"] == native_ids["codex"]
                deliveries.append(("codex", method))
                return {"thread": {"id": native_ids["codex"]}}
            if method == "turn/start":
                assert params["effort"] == "high"
                assert all(log not in json.dumps(params) for log in private_logs.values())
                name, arguments = command("codex", phase != "initial")
                await self.events.put(
                    {
                        "id": 1,
                        "method": "item/tool/call",
                        "params": {
                            "threadId": native_ids["codex"],
                            "turnId": "turn",
                            "callId": "call",
                            "tool": name,
                            "arguments": arguments,
                        },
                    }
                )
                return {"turn": {"id": "turn"}}
            return {}

        async def send(self, message):
            assert message["result"]["success"], message
            await self.events.put(
                {"method": "item/agentMessage/delta", "params": {"delta": private_logs["codex"]}}
            )
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn", "status": "completed"}},
                }
            )

    class AgYRPC:
        OPTIONS = [
            {
                "id": "model",
                "category": "model",
                "currentValue": "other",
                "options": [{"value": "gemini-test", "name": "Gemini Test"}],
            },
            {
                "id": "thought",
                "category": "thought_level",
                "currentValue": "high",
                "options": [{"value": "low", "name": "Low"}],
            },
            {"id": "mode", "category": "mode", "options": [{"value": "default"}]},
        ]

        def __init__(self, *_):
            self.events = asyncio.Queue()
            self.approval = asyncio.Event()
            self.initialized = {"agentCapabilities": {"loadSession": True}}
            # Display the model's thinking options both in discovery and after model selection.
            self.options = json.loads(json.dumps(self.OPTIONS))
            self.options[0]["currentValue"] = "gemini-test"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def request(self, method, params, timeout=None):  # noqa: ASYNC109
            if method in {"session/new", "session/load"}:
                self.bridge = params["mcpServers"]
                if self.bridge:
                    deliveries.append(("antigravity", method))
                if method == "session/load":
                    assert params["sessionId"] == native_ids["antigravity"]
                return {"sessionId": native_ids["antigravity"], "configOptions": self.options}
            if method == "session/set_config_option":
                assert params["value"] in {"gemini-test", "low", "default"}
                option = next(item for item in self.options if item["id"] == params["configId"])
                option["currentValue"] = params["value"]
                return {"configOptions": self.options}
            if method == "session/prompt":
                assert self.options[1]["currentValue"] == "low"
                assert all(log not in json.dumps(params) for log in private_logs.values())
                name, arguments = command("antigravity", phase != "initial")
                call = {
                    "toolCallId": "platform-tool",
                    "name": f"mcp_{self.bridge[0]['name']}_{name}",
                    "kind": "other",
                }
                await self.events.put(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": native_ids["antigravity"],
                            "update": {"sessionUpdate": "tool_call", **call},
                        },
                    }
                )
                await self.events.put(
                    {
                        "id": 77,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": native_ids["antigravity"],
                            "toolCall": call,
                            "options": [{"kind": "allow_once", "optionId": "allow"}],
                        },
                    }
                )
                await self.approval.wait()
                env = {item["name"]: item["value"] for item in self.bridge[0]["env"]}
                reader, writer = await asyncio.open_unix_connection(env["CHEM_MCP_SOCKET"])
                writer.write(
                    (
                        json.dumps(
                            {
                                "token": env["CHEM_MCP_TOKEN"],
                                "message": {
                                    "id": 1,
                                    "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments},
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                reply = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                assert not reply["result"]["isError"], reply
                await self.events.put(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": native_ids["antigravity"],
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": private_logs["antigravity"]},
                            },
                        },
                    }
                )
                return {"stopReason": "end_turn"}
            return {}

        async def send(self, response):
            if response.get("id") == 77:
                assert response["result"]["outcome"]["optionId"] == "allow"
                self.approval.set()

    monkeypatch.setattr(CodexBackend, "executable", staticmethod(lambda _: "/mock/codex"))
    monkeypatch.setattr(AntigravityBackend, "executable", staticmethod(lambda _: "/mock/agy"))

    parent = None
    for phase in ("initial", "restart"):
        app = create_app(settings, transport=httpx.MockTransport(model_reply))
        async with app.router.lifespan_context(app):
            plans = app.state.calculation_chat.plans
            await plans.close()
            plans.slurm = LocalSlurm(plans.chat)
            app.state.agent_backends.codex.transport_factory = CodexRPC
            app.state.agent_backends.antigravity.transport_factory = AgYRPC
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                if phase == "initial":
                    parent = (await client.post("/sessions", json={})).json()["id"]
                    base = f"/api/automation/chat/{parent}/workflows"
                    response = await client.post(
                        base,
                        json={
                            "title": "多 Agent",
                            "goal": "验证三条恢复路径",
                            "nodes": [
                                {
                                    "id": backend,
                                    "kind": "task",
                                    "title": backend,
                                    "execution_host": "test",
                                }
                                for backend in private_logs
                            ],
                        },
                    )
                    assert response.is_success, response.text
                    plan = response.json()
                    plan_id = plan["id"]
                    for backend, effort, model in [
                        ("codex", "high", "codex-test"),
                        ("antigravity", "low", "gemini-test"),
                    ]:
                        preset_id = str(uuid4())
                        payload = {
                            "name": backend,
                            "instance_id": f"local-{backend}",
                            "backend": backend,
                            "model": model,
                            "effort": effort,
                        }
                        response = await client.put(
                            f"/api/agents/presets/{preset_id}", json=payload
                        )
                        assert response.is_success, response.text
                        response = await client.put(
                            f"{base}/{plan_id}/steps/{backend}/agent",
                            json={
                                "expected_revision": plan["revision"],
                                "preset_id": preset_id,
                                "preset_version": 1,
                            },
                        )
                        assert response.is_success, response.text
                        plan = response.json()
                    response = await client.post(
                        f"{base}/{plan_id}/confirm", json={"revision": plan["revision"]}
                    )
                    assert response.is_success, response.text
                    await plans.tick_plan(parent, plan_id)
                    tasks = plans.dispatcher.tasks_for(parent, plan_id)
                    assert len(tasks) == 2
                    for backend in private_logs:
                        async with asyncio.timeout(10):
                            while True:
                                response = await client.get(
                                    f"{base}/{plan_id}/steps/{backend}/thread"
                                )
                                assert response.is_success, response.text
                                task_thread = response.json()
                                if task_thread["approvals"]:
                                    break
                                await asyncio.sleep(0.02)
                        item = task_thread["approvals"][0]
                        inbox = await client.get(f"{base}/{plan_id}/attention")
                        assert inbox.is_success, inbox.text
                        assert any(
                            pending["node_id"] == backend and pending["approval_id"] == item["id"]
                            for pending in inbox.json()["items"]
                        )
                        wrong_thread = await client.post(
                            f"/api/agents/sessions/{parent}/approvals/{item['id']}",
                            json={"approved": True},
                        )
                        assert wrong_thread.status_code == 409
                        assert (
                            await client.post(
                                f"/api/agents/sessions/{task_thread['id']}/approvals/{item['id']}",
                                json={"approved": True},
                            )
                        ).is_success
                        repeated = await client.post(
                            f"/api/agents/sessions/{task_thread['id']}/approvals/{item['id']}",
                            json={"approved": True},
                        )
                        assert repeated.status_code == 409
                    await asyncio.wait_for(asyncio.gather(*tasks), 10)
                    inbox = await client.get(f"{base}/{plan_id}/attention")
                    assert inbox.json()["items"] == []
                    await tick(plans, ToolContext(parent, "fixture"), plan_id)
                    assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
                else:
                    base = f"/api/automation/chat/{parent}/workflows"
                    task = task_session(parent, plan_id, "codex")
                    token = current_task_agent.set(TaskAgentScope(parent, plan_id, "codex", task))
                    try:
                        _, original_args = command("codex", False)
                        replay = await submit_task_job(
                            plans, TaskSubmission(**original_args), ToolContext(parent, "duplicate")
                        )
                        assert replay["status"] == "running"
                    finally:
                        current_task_agent.reset(token)
                    # Exercise native recovery one at a time here to give each mock
                    # report the current plan revision. Parallel dispatch is tested above.
                    async def defer_delivery(*_):
                        pass

                    with monkeypatch.context() as controlled:
                        controlled.setattr(plans.dispatcher, "offer", defer_delivery)
                        plan = await tick(plans, ToolContext(parent, "fixture"), plan_id)
                    await wake(plans, parent, plan_id)
                    plan = plans.chat.load(parent, "plans", plan_id)
                    await wake(plans, parent, plan_id)
                    await wake(plans, parent, plan_id)  # Repeated offer of the same terminal state.
                    assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
                    plan = await tick(plans, ToolContext(parent, "fixture"), plan_id)
                    assert plan["status"] == "completed"
                    assert all(
                        plan["runtime"][backend]["status"] == "completed"
                        for backend in private_logs
                    )
                    threads = {}
                    for backend in private_logs:
                        response = await client.get(f"{base}/{plan_id}/steps/{backend}/thread")
                        assert response.is_success, response.text
                        threads[backend] = response.json()
                        assert threads[backend]["external_thread_id"] == native_ids[backend]
                        assert len(threads[backend]["runs"]) == 2
                        assert all(
                            run["answer"] == private_logs[backend]
                            for run in threads[backend]["runs"]
                        )
                    assert len({item["id"] for item in threads.values()}) == 2
                    main_session = await client.get(f"/sessions/{parent}")
                    assert main_session.is_success, main_session.text
                    assert all(log not in main_session.text for log in private_logs.values())
                    response = await client.post(
                        "/chat", json={"session_id": parent, "message": "请规划下一步"}
                    )
                    assert response.is_success, response.text
                    assert all(log not in json.dumps(main_inputs) for log in private_logs.values())
                    assert [item["id"] for item in await app.state.store.list_sessions(20, 0)] == [
                        parent
                    ]

    assert sorted(deliveries) == sorted([
        ("codex", "thread/start"),
        ("antigravity", "session/new"),
        ("codex", "thread/resume"),
        ("antigravity", "session/load"),
    ])
