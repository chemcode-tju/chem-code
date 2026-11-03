import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app


def test_frontend_assets_and_api_are_served_together(settings_factory):
    app = create_app(
        settings_factory(), transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Chem Code" in response.text
        assert 'id="chat-form"' in response.text
        assert "test-only-key" not in response.text
        for asset in (
            "styles.css",
            "app.mjs",
            "core.mjs",
            "choices.css",
            "choices.mjs",
            "inspector-tabs.mjs",
            "ui-icons.svg",
            "favicon.svg",
            "vendor/markdown-it.min.js",
        ):
            response = client.get("/assets/" + asset)
            assert response.status_code == 200
            assert "test-only-key" not in response.text
        assert client.get("/assets/app.mjs").headers["content-type"].split(";")[0] in {
            "application/javascript",
            "text/javascript",
        }
        assert client.get("/docs").status_code == 200
        assert client.get("/.env").status_code == 404
        assert client.get("/assets/%2e%2e/%2e%2e/.env").status_code == 404


def test_auto_title_uses_conversation_and_preserves_curated_names(settings_factory):
    import json

    prompts = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        is_title = "sidebar title" in body["messages"][0]["content"]
        if is_title:
            prompts.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Pd 表面吸附位点" if is_title else "对比了桥位与顶位。",
                        },
                    }
                ]
            },
        )

    with TestClient(
        create_app(settings_factory(), transport=httpx.MockTransport(respond))
    ) as client:
        for seed in ({}, {"title": "我的笔记"}):
            session = client.post("/sessions", json=seed).json()["id"]
            ask = "请帮我研究一下钯表面的吸附位点，想对比桥位和顶位的差异。"
            assert (
                client.post("/chat", json={"session_id": session, "message": ask}).status_code
                == 200
            )
            for _ in range(2):
                title = client.post(f"/sessions/{session}/title").json()["title"]
                assert title == (seed.get("title") or "Pd 表面吸附位点")
            assert client.get(f"/sessions/{session}").json()["messages"][0]["content"] == ask
        assert len(prompts) == 1 and not prompts[0].get("tools")
