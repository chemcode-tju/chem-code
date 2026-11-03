"""Conversational setup uses the same server registration and test operations as settings."""

from uuid import UUID

from chemistry_agent.automation.models import ServerCreate, StrictModel
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.automation.settings_tools import configuration_grant
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext


class ServerTest(StrictModel):
    server_id: UUID


def connection_tools(service: AutomationService) -> list[Tool]:
    async def register(args: ServerCreate, context: ToolContext) -> dict:
        if not configuration_grant.get():
            raise AgentError("settings_grant_required", "请在本轮明确说出要登记的服务器。", 403)
        return await service.call("create_server", args.model_dump(mode="json"))

    async def test(args: ServerTest, context: ToolContext) -> dict:
        return await service.call("test_server", {"server_id": str(args.server_id)})

    return [
        Tool(
            "ssh_server_register",
            "Register a local SSH connection on the user's setup request. "
            "Reference an SSH alias or local identity path. Never request passwords or key content "
            "in chat. For trust or password entry point the user to /settings#servers.",
            ServerCreate,
            register,
            True,
        ),
        Tool(
            "ssh_server_test",
            "Check SSH login, scheduler and work directory for the registered "
            "server without submitting a job.",
            ServerTest,
            test,
            True,
        ),
    ]
