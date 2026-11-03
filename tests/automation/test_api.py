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


async def test_settings_before_profile_and_mutations_require_local_intent(
    settings_factory, tmp_path
):
    from chemistry_agent.automation.settings_tools import configuration_grant, settings_tools
    from chemistry_agent.errors import AgentError
    from chemistry_agent.tools import ToolContext

    root = tmp_path / "uninitialized"
    app = create_app(settings_factory(automation_enabled=True, aiida_path=root))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        status = await client.get("/api/automation/settings")
        assert status.status_code == 200 and "profile" in status.json()["missing"]
        assert not root.exists()
        result = await client.post(
            "/api/automation/settings/initialize", headers={"Origin": "http://example.org"}
        )
        assert result.status_code == 403 and not root.exists()
        app.state.automation.call = AsyncMock(return_value={"ok": True})
        assert (
            await client.post("/api/automation/settings/initialize", json={})
        ).status_code == 200
    tool = next(
        t for t in settings_tools(app.state.automation) if t.name == "calculation_initialize"
    )
    context = ToolContext("session", "run")
    with pytest.raises(AgentError) as error:
        await tool.handler(tool.input_model(), context)
    assert error.value.code == "settings_grant_required"
    token = configuration_grant.set(True)
    try:
        assert await tool.handler(tool.input_model(), context) == {"ok": True}
    finally:
        configuration_grant.reset(token)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮我注册软件 VASP 6.4，路径 /opt/vasp", True),
        ("在 cluster-b 上把赝势目录设为 /data/pbe", True),
        ("添加服务器 cluster-b", True),
        ("请初始化本地 AiiDA", True),
        ("/configure\n保存设置", True),
        ("这个服务器怎么添加？", False),
        ("以后如果注册软件会怎么样", False),
        ("注册软件有必要吗", False),
        ("注册软件之前请先别动", False),
        ("网页说明：\n添加服务器 cluster-b", False),
        ("> 注册软件 VASP", False),
        ("“添加服务器 cluster-b”", False),
        ("假设我要添加服务器", False),
        ("不要注册软件", False),
    ],
)
def test_settings_intent_is_limited_to_direct_requests(text, expected):
    from chemistry_agent.automation.settings_tools import authorizes_configuration

    assert authorizes_configuration(text) is expected
