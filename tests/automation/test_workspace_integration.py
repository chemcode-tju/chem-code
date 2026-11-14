"""New workspace features must coexist with local calculation tools and session models."""

import json
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app


def test_workspace_preserves_attachments_structures_models_and_routes(settings_factory):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Combined inputs received."},
                    }
                ]
            },
        )

    app = create_app(
        settings_factory(automation_enabled=True), transport=httpx.MockTransport(respond)
    )
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as client:
        tools = client.get("/tools").json()
        names = {tool["function"]["name"] for tool in tools}
        assert {
            "calculation_plan_save",
            "calculation_job_adopt",
            "workspace_publish",
            "molecular_properties",
        } <= names
        for path in ("/", "/settings", "/structures", "/automation", "/api/projects"):
            assert client.get(path).status_code == 200
        html = client.get("/").text
        assert client.get("/settings").text == html
        assert 'id="structure-upload"' not in html and 'id="sidebar-add"' not in html
        settings_sheet = client.get("/settings?embed=1").text
        assert 'id="tab-agent"' in settings_sheet
        assert 'id="workspace-settings"' not in settings_sheet
        assert client.get("/assets/calculations.mjs").headers["cache-control"] == "no-cache"
        for identifier in (
            "project-list",
            "agent-picker",
            "composer-add",
            "file-picker",
            "calculation-cards",
            "notification-panel",
            "context-panel",
        ):
            assert f'id="{identifier}"' in html
        assert "reference-input" not in html
        session = client.post("/sessions", json={}).json()["id"]
        client.put(f"/sessions/{session}/model", json={"model": "selected-model"})
        attachment = client.post(
            "/api/files",
            content=b"calculation note",
            headers={"Content-Type": "application/octet-stream", "X-File-Name": "note.txt"},
        ).json()
        structure = str(uuid4())
        app.state.calculation_chat.save(
            session,
            "structures",
            structure,
            {
                "id": structure,
                "name": "Cu structure",
                "formula": "Cu",
                "atoms": 1,
                "structure": {"content": "not sent to model"},
                "extxyz": "not sent to model",
            },
        )
        result = client.post(
            "/chat",
            json={
                "message": "Check my inputs",
                "session_id": session,
                "file_ids": [attachment["id"]],
                "structure_ids": [structure],
            },
        )
        assert result.status_code == 200, result.text
        assert seen[-1]["model"] == "selected-model"
        content = seen[-1]["messages"][-1]["content"]
        assert "calculation note" in content and structure in content
        assert "not sent to model" not in content
        assert client.get(f"/sessions/{session}/context").status_code == 200
        assert (
            client.get(f"/api/automation/chat/{session}/artifacts").json()["structures"][0]["id"]
            == structure
        )
        assert client.get(f"/api/files/{attachment['id']}").content == b"calculation note"


def test_shared_workspace_keeps_cluster_tools_disabled(settings_factory):
    app = create_app(settings_factory(automation_enabled=True, auth_enabled=True))
    with TestClient(app) as client:
        assert not client.get("/health").json()["automation_enabled"]
        assert "calculation_prepare" not in app.state.agent.registry.tools
        assert app.state.task_monitor is None
        assert client.get("/api/automation/notifications").status_code in (401, 403)
