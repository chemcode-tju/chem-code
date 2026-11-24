"""Stateless MCP endpoint for external agents; identity comes from the platform.

Reach a remote platform through the same authenticated SSH tunnel as the web UI.
No model's native session ID or private conversation state is part of this API.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from chemistry_agent.automation.routes import local_service
from chemistry_agent.errors import AgentError
from chemistry_agent.knowledge.assets import ASSET_TOOLS
from chemistry_agent.knowledge.config import KnowledgeQuery, KnowledgeRead, KnowledgeWrite
from chemistry_agent.knowledge.plugins import SkillQuery, SkillRead
from chemistry_agent.memory.models import MemoryRead, MemorySave, MemorySearch
from chemistry_agent.memory.notes import save_note
from chemistry_agent.memory.scope import MemoryScope
from chemistry_agent.memory.service import ResearchMemory
from chemistry_agent.projects import require_project

router = APIRouter()
INPUTS = {
    "knowledge_search": KnowledgeQuery,
    "knowledge_read": KnowledgeRead,
    "knowledge_write": KnowledgeWrite,
    "skill_list": SkillQuery,
    "skill_read": SkillRead,
    "memory_search": MemorySearch,
    "memory_read": MemoryRead,
    "memory_save": MemorySave,
    **{name: entry[0] for name, entry in ASSET_TOOLS.items()},
}


async def invoke(service, scope: MemoryScope, name: str, arguments: dict) -> dict:
    if name not in INPUTS:
        raise AgentError("tool_not_found", "知识工具不存在。", 404)
    args = INPUTS[name].model_validate(arguments)
    if name in ASSET_TOOLS:
        return await getattr(service.assets, ASSET_TOOLS[name][2])(scope, args)
    if name == "knowledge_search":
        return await service.search(scope, args)
    if name == "knowledge_read":
        return await service.read(service.scope(scope, args.scope), args.path, args.level)
    if name == "knowledge_write":
        return await service.write(service.scope(scope, args.scope), args.path, args.content)
    if name == "skill_list":
        return await asyncio.to_thread(service.plugins.catalog, args.query)
    if name == "skill_read":
        return await asyncio.to_thread(service.plugins.read, args)
    memory = ResearchMemory(service.store)
    if name == "memory_search":
        return await memory.search(scope, args)
    if name == "memory_read":
        item = await memory.read(scope, str(args.memory_id))
        content = item["content"]
        return item | {
            "content": content[args.offset : args.offset + args.length],
            "total_characters": len(content),
        }
    return await save_note(memory, scope, args)


@router.post("/api/knowledge/mcp")
async def endpoint(request: Request, project_id: UUID | None = None):
    local_service(request)
    service = request.app.state.knowledge
    if project_id:
        async with service.store.lock:
            await require_project(service.store, str(project_id), request.state.user_id)
    scope = MemoryScope(request.state.user_id, str(project_id) if project_id else None)
    message = await request.json()
    identifier, method = message.get("id"), message.get("method")
    if identifier is None:
        return Response(status_code=202)
    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "chem-research-knowledge", "version": "1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": ASSET_TOOLS[name][1]
                    if name in ASSET_TOOLS
                    else name.replace("_", " ")
                    + ": shared project knowledge; read evidence before reuse.",
                    "inputSchema": model.model_json_schema(),
                }
                for name, model in INPUTS.items()
            ]
        }
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            value = await invoke(service, scope, params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        except (AgentError, ValueError, ValidationError) as exc:
            result = {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": exc.message
                        if isinstance(exc, AgentError)
                        else "Invalid tool arguments",
                    }
                ],
            }
    else:
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def main() -> None:
    """Optional stdio adapter for hosts without Streamable HTTP support."""
    url = httpx.URL(os.environ.get("CHEM_KNOWLEDGE_URL", "http://127.0.0.1:8766/api/knowledge/mcp"))
    headers = {}
    if token_file := os.environ.get("CHEM_KNOWLEDGE_TOKEN_FILE"):
        if url.scheme != "http" or url.host != "127.0.0.1" or url.path != "/api/knowledge/mcp":
            raise SystemExit("The desktop MCP token is only for the local knowledge endpoint.")
        headers["Authorization"] = f"Bearer {Path(token_file).read_text().strip()}"
    # This is an explicit platform connection (often an SSH loopback tunnel).
    # Do not route project knowledge through inherited system HTTP proxies.
    with httpx.Client(timeout=60, trust_env=False, headers=headers) as client:
        for line in sys.stdin:
            message = json.loads(line)
            response = client.post(url, json=message)
            response.raise_for_status()
            if response.content:
                print(response.text, flush=True)


if __name__ == "__main__":
    main()
