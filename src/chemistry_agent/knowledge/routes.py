"""Shared knowledge settings and tools; local administration uses the existing boundary."""

import asyncio
import sys
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError

from chemistry_agent.automation.routes import local_service
from chemistry_agent.knowledge.config import KnowledgeUpdate
from chemistry_agent.knowledge.plugins import OFFICIAL_SOURCES, PluginInstall, SkillRead
from chemistry_agent.knowledge.tools import knowledge_tools
from chemistry_agent.tools import ToolContext

router = APIRouter(prefix="/api/knowledge", tags=["knowledge and skills"])
MCP_MODULE_ROOT = str(Path(__file__).resolve().parents[2])


def service(request: Request):
    local_service(request)
    return request.app.state.knowledge


@router.get("/settings")
async def settings(request: Request) -> dict:
    return service(request).config.public()


@router.get("/connection")
async def connection(request: Request) -> dict:
    service(request)
    server = request.scope.get("server")
    port = server[1] if server else (request.url.port or 80)
    environment = {"PYTHONPATH": MCP_MODULE_ROOT}
    if token_file := getattr(request.state, "desktop_mcp_token_file", None):
        environment["CHEM_KNOWLEDGE_TOKEN_FILE"] = token_file
    return {
        "http_path": "/api/knowledge/mcp",
        "stdio_command": sys.executable,
        "stdio_args": ["-m", "chemistry_agent.knowledge.mcp"],
        "stdio_env": environment,
        "platform_origin": f"http://127.0.0.1:{port}",
    }


@router.put("/settings")
async def save(body: KnowledgeUpdate, request: Request) -> dict:
    return service(request).config.save(body)


@router.post("/probe")
async def probe(request: Request) -> dict:
    return await service(request).client.probe()


@router.post("/sync")
async def sync(request: Request) -> dict:
    return await service(request).sync.sync()


@router.get("/plugins")
async def plugins(request: Request) -> dict:
    return {"packages": service(request).plugins.packages(), "sources": OFFICIAL_SOURCES}


@router.post("/plugins")
async def install(body: PluginInstall, request: Request) -> dict:
    return await service(request).plugins.install(body)


class Enable(BaseModel):
    enabled: bool


@router.put("/plugins/{identifier}")
async def enable(identifier: str, body: Enable, request: Request) -> dict:
    return await asyncio.to_thread(service(request).plugins.enable, identifier, body.enabled)


@router.post("/skills/read")
async def read_skill(body: SkillRead, request: Request) -> dict:
    return await asyncio.to_thread(service(request).plugins.read, body)


@router.get("/tools")
async def catalog(request: Request) -> dict:
    service(request)
    return {"tools": [tool.schema() for tool in knowledge_tools(lambda: service(request))]}


class ToolArguments(BaseModel):
    arguments: dict


@router.post("/sessions/{session_id}/tools/{name}")
async def execute(session_id: UUID, name: str, body: ToolArguments, request: Request) -> dict:
    from chemistry_agent.errors import AgentError

    knowledge = service(request)
    await knowledge.store.get_session(str(session_id), request.state.user_id)
    tool = next((t for t in knowledge_tools(lambda: knowledge) if t.name == name), None)
    if tool is None:
        raise AgentError("tool_not_found", "知识工具不存在。", 404)
    try:
        arguments = tool.input_model.model_validate(body.arguments)
    except ValidationError as exc:
        raise AgentError("invalid_tool_arguments", "工具参数格式不正确。", 422) from exc
    return await tool.handler(arguments, ToolContext(str(session_id), str(uuid4())))
