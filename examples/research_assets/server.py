"""Read-only Streamable HTTP MCP for the standalone research asset catalog."""

import base64
import json
from typing import Literal

from catalog import AssetUnavailable, Catalog, Fetch, Inspect, Search
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError

TOOLS = {
    "asset_search": (Search, "Find authorized fixed revisions by science fields and keywords."),
    "asset_inspect": (Inspect, "Read a fixed revision's origin, review, inputs and file digests."),
    "asset_fetch": (Fetch, "Fetch one reviewed file from a fixed revision with its SHA-256."),
}
PROTOCOLS = ("2025-03-26", "2025-06-18", "2025-11-25")


class Message(BaseModel):
    jsonrpc: Literal["2.0"]
    id: int | str | None = None
    method: str
    params: dict = Field(default_factory=dict)


def call(catalog: Catalog, group: str, name: str, arguments: dict) -> dict:
    args = TOOLS[name][0].model_validate(arguments)
    if isinstance(args, Search):
        return {"hits": catalog.search(group, args)}
    if isinstance(args, Fetch):
        data, digest = catalog.fetch(group, args.revision, args.name)
        return {
            "revision": args.revision,
            "name": args.name,
            "sha256": digest,
            "content_base64": base64.b64encode(data).decode("ascii"),
        }
    return catalog.inspect(group, args.revision)


def create_app(catalog: Catalog, tokens: dict[str, str]) -> FastAPI:
    app = FastAPI(title="Chem-Agent research asset demo")

    @app.post("/mcp", response_model=None)
    def endpoint(
        message: Message,
        authorization: str = Header(default=""),
        origin: str | None = Header(default=None),
    ) -> dict | Response:
        # Demo clients are local Agent processes. Do not accept browser cross-origin traffic.
        if origin:
            raise HTTPException(403, "Origin is not allowed")
        scheme, _, token = authorization.partition(" ")
        group = tokens.get(token) if scheme.casefold() == "bearer" else None
        if group is None:
            raise HTTPException(401, "Invalid demo credential", {"WWW-Authenticate": "Bearer"})
        if message.id is None:
            return Response(status_code=202)

        reply = {"jsonrpc": "2.0", "id": message.id}
        if message.method == "initialize":
            requested = message.params.get("protocolVersion")
            result = {
                "protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "chem-research-assets-demo", "version": "0.1.0"},
            }
        elif message.method == "ping":
            result = {}
        elif message.method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": desc, "inputSchema": model.model_json_schema()}
                    for name, (model, desc) in TOOLS.items()
                ]
            }
        elif message.method == "tools/call":
            try:
                value = call(
                    catalog,
                    group,
                    message.params.get("name", ""),
                    message.params.get("arguments", {}),
                )
                result = {
                    "structuredContent": value,
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                }
            except AssetUnavailable as exc:
                result = tool_error(str(exc))
            except (ValidationError, KeyError, TypeError):
                result = tool_error("Unknown tool or invalid arguments")
            except ValueError:
                result = tool_error("The stored payload failed integrity verification")
        else:
            return reply | {"error": {"code": -32601, "message": "Method not found"}}
        return reply | {"result": result}

    @app.get("/mcp", include_in_schema=False)
    def no_event_stream() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    return app


def tool_error(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}
