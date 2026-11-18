"""One settings surface for execution locations; no per-conversation host restriction."""

import asyncio
import platform

from fastapi import APIRouter, Request
from pydantic import BaseModel

from chemistry_agent.automation.routes import local_service
from chemistry_agent.environments.registry import Environment, Environments

router = APIRouter(prefix="/api/environments", tags=["execution environments"])


def environments(request: Request) -> Environments:
    local_service(request)
    return Environments(request.app.state.settings.platform_path)


@router.get("")
async def list_environments(request: Request) -> dict:
    registry = environments(request)
    defaults = request.app.state.settings.allowed_ssh_hosts
    rows, choices = await asyncio.gather(
        asyncio.to_thread(registry.inventory, defaults),
        asyncio.to_thread(registry.ssh_choices, defaults),
    )
    task = request.app.state.calculation_chat.plans.task
    return {
        "environments": rows,
        "ssh_choices": choices,
        "platform_host": platform.node(),
        "agent_host": platform.node(),
        "monitor_host": platform.node(),
        "monitor_running": bool(task and not task.done()),
    }


@router.post("")
async def create(body: Environment, request: Request) -> dict:
    return await asyncio.to_thread(
        environments(request).save,
        body,
        create=True,
        defaults=request.app.state.settings.allowed_ssh_hosts,
    )


@router.put("/{identifier}")
async def save(identifier: str, body: Environment, request: Request) -> dict:
    from chemistry_agent.errors import AgentError

    if identifier != body.id:
        raise AgentError("environment_id_mismatch", "环境标识不一致。", 422)
    return await asyncio.to_thread(
        environments(request).save, body, defaults=request.app.state.settings.allowed_ssh_hosts
    )


class Enable(BaseModel):
    enabled: bool


@router.patch("/{identifier}")
async def enable(identifier: str, body: Enable, request: Request) -> dict:
    from chemistry_agent.errors import AgentError

    registry = environments(request)
    defaults = request.app.state.settings.allowed_ssh_hosts
    row = next((row for row in registry.inventory(defaults) if row["id"] == identifier), None)
    if not row:
        raise AgentError("environment_missing", "节点不存在。", 404)
    value = registry.get(identifier) or Environment.model_validate(
        {k: v for k, v in row.items() if k in Environment.model_fields}
    )
    return await asyncio.to_thread(
        registry.save, value.model_copy(update={"enabled": body.enabled}), defaults=defaults
    )


@router.delete("/{identifier}")
async def remove(identifier: str, request: Request) -> dict:
    await asyncio.to_thread(
        environments(request).remove, identifier, request.app.state.settings.allowed_ssh_hosts
    )
    return {"removed": True}


@router.post("/{identifier}/probe")
async def probe(identifier: str, request: Request) -> dict:
    registry = environments(request)
    record = registry.get(identifier)
    if record and record.transport != "ssh":
        return await registry.request(identifier, "probe") | {"ok": True}
    from chemistry_agent.runtime.shell import ShellInput, remote_shell_tool
    from chemistry_agent.tools import ToolContext

    tool = remote_shell_tool(request.app.state.settings.allowed_ssh_hosts, registry.platform_root)
    return await tool.handler(
        ShellInput(
            host=identifier,
            command="hostname; command -v sbatch || true; command -v squeue || true",
        ),
        ToolContext("connection-test", "connection-test"),
    )
