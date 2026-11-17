"""Host → Agent instances and models; working folders belong to conversations."""

import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, Query, Request

from chemistry_agent.automation.routes import local_service
from chemistry_agent.backends.service import BackendSettings, SessionSelection
from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.connection import directories, discover
from chemistry_agent.hosting.native import native_scope
from chemistry_agent.hosting.registry import AgentInstance, validate_directory

router = APIRouter(prefix="/api/hosting", tags=["Agent locations"])


def service(request: Request):
    local_service(request)
    return request.app.state.agent_backends


@router.get("")
async def inventory(request: Request) -> dict:
    backends = service(request)
    config = await backends.settings()
    return {
        "hosts": await asyncio.to_thread(backends.hosting.hosts),
        "agents": backends.hosting.agents(config),
    }


@router.get("/monitors")
async def monitors(request: Request) -> dict:
    backends = service(request)

    def read() -> list[dict]:
        groups = {}
        for path in backends.hosting.environments.platform_root.glob("chat/*/slurm_jobs/*.json"):
            record = json.loads(path.read_text())
            if record.get("terminal"):
                continue
            owner = record.get("monitor", {"host": "local"})
            host = owner["host"]
            row = groups.setdefault(host, {"host": host, "jobs": 0, "errors": [], "handoffs": 0})
            row["jobs"] += 1
            row["handoffs"] += bool(owner.get("handoff"))
            if owner.get("error") and owner["error"] not in row["errors"]:
                row["errors"].append(owner["error"])
        return list(groups.values())

    return {"monitors": await asyncio.to_thread(read)}


@router.post("/hosts/{host}/discover")
async def detect(host: str, request: Request) -> dict:
    backends = service(request)
    result = await discover(backends.hosting, host)
    existing = backends.hosting.agents(await backends.settings())
    for row in result["agents"]:
        # Discovery is idempotent and never overwrites a manually configured instance.
        if not any(a["host_id"] == host and a["backend"] == row["backend"] for a in existing):
            await asyncio.to_thread(
                backends.hosting.save,
                AgentInstance(id=f"{host}-{row['backend']}", host_id=host, **row),
            )
    return result | await inventory(request)


@router.put("/agents/{identifier}")
async def save_agent(identifier: str, body: AgentInstance, request: Request) -> dict:
    if identifier != body.id:
        raise AgentError("agent_id_mismatch", "Agent 标识不一致。", 422)
    return await asyncio.to_thread(service(request).hosting.save, body)


@router.get("/hosts/{host}/directories")
async def browse_directories(
    host: str, request: Request, path: str = Query(default="", max_length=2048)
) -> dict:
    backends = service(request)
    try:
        path = validate_directory(path)
    except ValueError as exc:
        raise AgentError("directory_invalid", str(exc), 422) from exc
    return await directories(backends.hosting, host, path)


@router.get("/agents/{identifier}/models")
async def models(identifier: str, request: Request) -> dict:
    backends = service(request)
    config = await backends.settings()
    item = next((a for a in backends.hosting.agents(config) if a["id"] == identifier), None)
    if not item:
        raise AgentError("agent_instance_missing", "Agent 实例不存在。", 404)
    if item["backend"] == "builtin":
        provider = request.app.state.provider
        return await provider.list_models() | {"configured": await provider.ready()}
    resolved = backends.hosting.resolve(
        SessionSelection(backend=item["backend"], instance_id=identifier, permission="user"), config
    )
    config = BackendSettings.model_validate(config.model_dump() | resolved)
    async with native_scope(backends, config, f"probe-{uuid4()}", item["backend"]):
        result = await backends.adapters[item["backend"]].probe(config)
    return result | {"host_id": item["host_id"], "instance_id": identifier}
