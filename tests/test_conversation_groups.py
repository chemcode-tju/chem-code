"""Exercise group routing through real receipts, native delivery and built-in inference."""

import asyncio
import json
from uuid import uuid4

import httpx

from chemistry_agent.api import create_app
from chemistry_agent.backends.service import BackendSettings
from chemistry_agent.schemas import ToolExecution


class NativeSpeaker:
    def __init__(self, name: str, log: list) -> None:
        self.name, self.log = name, log

    async def probe(self, config: BackendSettings) -> dict:
        return {"authenticated": True, "models": [{"id": self.name}]}

    async def run(self, messages: list, progress, *, binding: dict, **kwargs) -> dict:
        self.log.append((self.name, messages[-1]["content"]))
        assert binding["external_thread_id"] is None
        execution = ToolExecution(
            call_id="fixture-" + self.name,
            name="check",
            arguments={},
            success=True,
            result={"evidence": "passed-" + self.name},
        )
        progress.tool_calls.append(execution)
        return {
            "messages": [*messages, {"role": "assistant", "content": "reply-" + self.name}],
            "tool_calls": progress.tool_calls,
            "usage": progress.current_usage,
        }


async def test_group_handoff_retry_and_individual_chat(settings_factory):
    calls = []
    failing = True

    def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "review-model"}]})
        payload = json.loads(request.content)
        assert all("group_messages" not in message for message in payload["messages"])
        calls.append((payload["model"], payload["messages"][-1]["content"]))
        if failing:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "verified"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = create_app(settings_factory(), transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        backends = app.state.agent_backends
        for name in ("antigravity", "codex"):
            backends.register(name, NativeSpeaker(name, calls))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            path = f"/api/agents/sessions/{session}"
            refs = []
            for name in ("antigravity", "codex", "builtin"):
                identifier = str(uuid4())
                preset = {
                    "name": name,
                    "backend": name,
                    "instance_id": "local-" + name,
                    "model": "review-model" if name == "builtin" else name,
                    "instructions": "role-" + name,
                }
                response = await client.put(f"/api/agents/presets/{identifier}", json=preset)
                assert response.is_success, response.text
                refs.append({"id": identifier, "version": 1})

            async def select(members: list) -> httpx.Response:
                return await client.put(path + "/group", json={"members": members})

            assert (await select(refs[:1])).status_code == 422
            assert (await select(refs[:1] * 2)).status_code == 422
            assert (await select([refs[0], refs[1] | {"version": 2}])).status_code == 409
            assert (await select(refs)).is_success
            assert [m["id"] for m in (await client.get(path)).json()["group"]] == [
                r["id"] for r in refs
            ]
            request = {
                "request_id": str(uuid4()),
                "session_id": session,
                "message": "check the update",
            }

            async def send(retry: bool = False) -> dict:
                response = await client.post(
                    "/chat/requests", params={"retry": retry}, json=request
                )
                assert response.is_success, response.text
                await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
                return (await client.get(f"/sessions/{session}/chat-requests")).json()["requests"][
                    -1
                ]

            failed = await send()
            assert failed["status"] == "failed"
            assert "reply-codex" in failed["partial_answer"]
            assert [item["member_id"] for item in failed["partial_group"]] == [
                ref["id"] for ref in refs[:2]
            ]
            assert [m for m, _ in calls] == ["antigravity", "codex", "review-model"]
            assert "reply-antigravity" in calls[1][1] and "passed-codex" in calls[2][1]
            assert "role-builtin" in calls[2][1]
            # Even after disabling the team a retry must resume the failed reviewer only.
            assert (await select([])).is_success
            failing = False
            recovered = await send(True)
            assert recovered["status"] == "completed"
            run = (await client.get("/runs/" + recovered["run_id"])).json()
            assert [speaker["preset_id"] for speaker in run["context"]["group_contributions"]] == [
                ref["id"] for ref in refs
            ]
            assert [m for m, _ in calls] == ["antigravity", "codex", "review-model", "review-model"]
            history = (await client.get(f"/sessions/{session}")).json()["messages"]
            assert history[0]["content"] == request["message"]
            assert [item["answer"] for item in history[-1]["group_messages"]] == [
                "reply-antigravity",
                "reply-codex",
                "verified",
            ]
            assert (
                history[-1]["content"] == "### antigravity\n\nreply-antigravity\n\n"
                "### codex\n\nreply-codex\n\n### builtin\n\nverified"
            )
            request.update(request_id=str(uuid4()), message="individual")
            assert (await send())["status"] == "completed"
            assert calls[-1] == ("deepseek-flash", "individual")


async def test_assigned_task_overrides_group_and_parent_events_use_team(settings_factory):
    """Confirm fixture-only tasks and deliver readiness events without any scheduler."""
    prompts = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": m} for m in ("plan", "review")]})
        payload = json.loads(request.content)
        prompts.append((payload["model"], {t["function"]["name"] for t in payload["tools"]}))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "checked fixture"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = create_app(
        settings_factory(automation_enabled=True), transport=httpx.MockTransport(provider)
    )
    async with app.router.lifespan_context(app):
        plans = app.state.calculation_chat.plans
        await plans.close()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            refs = []
            for model in ("plan", "review"):
                identifier = str(uuid4())
                response = await client.put(
                    f"/api/agents/presets/{identifier}",
                    json={
                        "name": model,
                        "backend": "builtin",
                        "model": model,
                        "instance_id": "local-builtin",
                    },
                )
                assert response.is_success, response.text
                refs.append({"id": identifier, "version": 1})
            route = f"/api/agents/sessions/{session}/group"
            assert (await client.put(route, json={"members": refs})).is_success
            for assigned in (True, False):
                prompts.clear()
                base = f"/api/automation/chat/{session}/workflows"
                plan = (
                    await client.post(
                        base,
                        json={
                            "title": "Fixture",
                            "goal": "Inspect fixture",
                            "nodes": [{"id": "inspect", "kind": "task", "title": "Inspect"}],
                        },
                    )
                ).json()
                path = base + "/" + plan["id"]
                if assigned:
                    response = await client.put(
                        path + "/steps/inspect/agent",
                        json={
                            "expected_revision": plan["revision"],
                            "preset_id": refs[1]["id"],
                            "preset_version": 1,
                        },
                    )
                    assert response.is_success, response.text
                    plan = response.json()
                response = await client.post(path + "/confirm", json={"revision": plan["revision"]})
                assert response.is_success, response.text
                await plans.tick_plan(session, plan["id"])
                await asyncio.wait_for(
                    asyncio.gather(*plans.dispatcher.tasks_for(session, plan["id"])), 5
                )
                assert [m for m, _ in prompts] == (["review"] if assigned else ["plan", "review"])
                assert all(("calculation_task_submit" in tools) == assigned for _, tools in prompts)


async def test_group_restart_resumes_current_member(settings_factory):
    settings = settings_factory()
    reached_review = asyncio.Event()
    interrupted = True
    models = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "planner"}, {"id": "reviewer"}]})
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "reviewer" and interrupted:
            reached_review.set()
            await asyncio.Future()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "done-" + model},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = create_app(settings, transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            members = []
            for model in ("planner", "reviewer"):
                identifier = str(uuid4())
                result = await client.put(
                    f"/api/agents/presets/{identifier}",
                    json={
                        "name": model,
                        "backend": "builtin",
                        "model": model,
                        "instance_id": "local-builtin",
                    },
                )
                assert result.is_success
                members.append({"id": identifier, "version": 1})
            route = f"/api/agents/sessions/{session}/group"
            assert (await client.put(route, json={"members": members})).is_success
            receipt = {"session_id": session, "request_id": str(uuid4()), "message": "inspect"}
            assert (await client.post("/chat/requests", json=receipt)).is_success
            await asyncio.wait_for(reached_review.wait(), 5)
            assert (await client.put(route, json={"members": []})).status_code == 409

    interrupted = False
    app = create_app(settings, transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 5)
        assert models == ["planner", "reviewer", "reviewer"]
        history = await app.state.store.get_session(session, None)
        assert "done-planner" in history["messages"][-1]["content"]
        assert "done-reviewer" in history["messages"][-1]["content"]


async def test_adaptive_team_can_return_work_and_resume_at_the_failed_visit(settings_factory):
    """A receipt must distinguish two visits to the same member."""
    turns = []
    fail_reviewer = True

    def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": [{"id": name} for name in ("writer", "critic")]}
            )
        data = json.loads(request.content)
        model = data["model"]
        turns.append(model)
        prompt = data["messages"][-1]["content"]
        can_transfer = any(t["function"]["name"] == "group_handoff" for t in data["tools"])
        if fail_reviewer and model == "critic" and "changes applied" in prompt:
            return httpx.Response(401)
        if model == "writer":
            target, task = (
                "critic",
                "changes applied" if "please revise" in prompt else "review first draft",
            )
        else:
            target, task = "writer", "please revise"
        if (
            not can_transfer
            or prompt.startswith("@critic ")
            or ("changes applied" in prompt and "loop" not in prompt)
        ):
            message = {"role": "assistant", "content": "done"}
        else:
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": uuid4().hex,
                        "type": "function",
                        "function": {
                            "name": "group_handoff",
                            "arguments": json.dumps({"target": target, "task": task}),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ]
            },
        )

    app = create_app(settings_factory(), transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            session = (await client.post("/sessions", json={})).json()["id"]
            refs = []
            for name in ("writer", "critic"):
                identifier = str(uuid4())
                response = await client.put(
                    f"/api/agents/presets/{identifier}",
                    json={
                        "name": name,
                        "backend": "builtin",
                        "model": name,
                        "instance_id": "local-builtin",
                    },
                )
                refs.append(
                    {"id": identifier, "version": response.raise_for_status().json()["version"]}
                )
            response = await client.put(
                f"/api/agents/sessions/{session}/group",
                json={"members": refs, "strategy": "handoff"},
            )
            response.raise_for_status()
            request = {"message": "draft", "request_id": str(uuid4()), "session_id": session}

            async def send(retry: bool = False) -> dict:
                (
                    await client.post("/chat/requests", params={"retry": retry}, json=request)
                ).raise_for_status()
                await asyncio.wait_for(asyncio.gather(*app.state.chat_queue.tasks), 6)
                return (await client.get(f"/sessions/{session}/chat-requests")).json()["requests"][
                    -1
                ]

            assert (await send())["status"] == "failed"
            assert turns == ["writer", "critic", "writer", "critic"]
            fail_reviewer = False
            recovered = await send(True)
            assert recovered["status"] == "completed" and turns.count("writer") == 2
            session_data = (await client.get(f"/sessions/{session}")).json()
            speeches = session_data["messages"][-1]["group_messages"]
            assert [m["name"] for m in speeches] == ["writer", "critic", "writer", "critic"]
            assert [m["handoff"]["task"] for m in speeches if m["handoff"]] == [
                "review first draft",
                "please revise",
                "changes applied",
            ]
            run = (await client.get("/runs/" + recovered["run_id"])).json()
            assert [x["name"] for x in run["context"]["group_contributions"]] == [
                "writer",
                "critic",
                "writer",
                "critic",
            ]
            for message, expected in [
                ("@critic check", ["critic"]),
                ("loop", ["writer", "critic", "writer", "critic", "writer"]),
            ]:
                turns.clear()
                request.update(message=message, request_id=str(uuid4()))
                assert (await send())["status"] == "completed"
                assert turns == expected
