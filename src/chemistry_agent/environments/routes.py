"""One settings surface for execution locations; no per-conversation host restriction."""

import platform

from fastapi import APIRouter, Request

from chemistry_agent.automation.routes import local_service
from chemistry_agent.environments.registry import Environment, Environments

router = APIRouter(prefix="/api/environments", tags=["execution environments"])


def environments(request: Request) -> Environments:
    local_service(request)
    return Environments(request.app.state.settings.platform_path)


@router.get("")
async def list_environments(request: Request) -> dict:
    return {
        "environments": environments(request).public(),
        "platform_host": platform.node(),
        "agent_host": platform.node(),
        "monitor_host": platform.node(),
    }


@router.put("/{identifier}")
async def save(identifier: str, body: Environment, request: Request) -> dict:
    from chemistry_agent.errors import AgentError

    if identifier != body.id:
        raise AgentError("environment_id_mismatch", "环境标识不一致。", 422)
    return environments(request).save(body)


@router.post("/{identifier}/probe")
async def probe(identifier: str, request: Request) -> dict:
    return await environments(request).request(identifier, "probe")
