from unittest.mock import AsyncMock

import httpx
import pytest

from chemistry_agent.api import create_app


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
            "/api/automation/configuration", headers={"Origin": origin} if origin else {}
        )
    assert response.status_code == 403
    app.state.automation.call.assert_not_called()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮我注册软件 VASP 6.4，路径 /opt/vasp", True),
        ("在 cluster-b 上把赝势目录设为 /data/pbe", True),
        ("添加服务器 cluster-b", True),
        ("你可以帮我添加一个新的服务器吗？", True),
        ("请把 cluster-b 上的赝势目录设置为 /pbe", True),
        ("Could you register a new server named cluster?", True),
        ("可以帮我说明怎么添加服务器吗？", False),
        ("请初始化本地 AiiDA", False),
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
