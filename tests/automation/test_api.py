from unittest.mock import AsyncMock

import httpx
import pytest

from chemistry_agent.api import create_app


@pytest.mark.asyncio
async def test_automation_is_opt_in(settings_factory):
    app = create_app(settings_factory())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/api/automation/status")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "automation_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url,origin,remote",
    [
        ("http://evil.example", None, "127.0.0.1"),
        ("http://127.0.0.1", "http://evil.example", "127.0.0.1"),
        ("http://127.0.0.1", None, "192.0.2.1"),
    ],
)
async def test_blocks_remote_and_cross_origin(settings_factory, base_url, origin, remote):
    app = create_app(settings_factory(automation_enabled=True))
    app.state.automation.call = AsyncMock()
    transport = httpx.ASGITransport(app=app, client=(remote, 1234))
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        response = await client.get(
            "/api/automation/status", headers={"Origin": origin} if origin else {}
        )
    assert response.status_code == 403
    app.state.automation.call.assert_not_called()


@pytest.mark.asyncio
async def test_local_preview_calls_worker_but_does_not_submit(settings_factory):
    app = create_app(settings_factory(automation_enabled=True))
    app.state.automation.call = AsyncMock(return_value={"preview_id": "example"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/automation/previews",
            json={
                "server_id": "518642db-13fb-4029-9c40-17a388234bf5",
                "structure": {"content": "some structure"},
                "resources": {"queue": "batch"},
            },
        )
        invalid = await client.post(
            "/api/automation/submissions", json={"preview_id": "../../secret"}
        )
    assert response.status_code == 200
    assert app.state.automation.call.call_args.args[0] == "preview"
    assert invalid.status_code == 422
    assert app.state.automation.call.call_count == 1
