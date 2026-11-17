"""Standard-library-only relay copied into each distributable local plugin."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


class Relay:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = json.loads((root / "connection.json").read_text())
        self.surface = self.config["surface"]
        self.uri = f"ui://chem-{self.surface}/panel.html"
        endpoint = urlsplit(self.config["url"])
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1":
            raise ValueError("此本地原型只允许 127.0.0.1；远端后台请通过 SSH 隧道连接")

    def request(self, path: str, arguments: dict | None = None) -> dict:
        token = Path(self.config["token_file"]).read_text().strip()
        request = Request(
            self.config["url"] + path,
            data=json.dumps(arguments).encode() if arguments is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=40) as response:
                return json.load(response)
        except HTTPError as error:
            raise ValueError(json.loads(error.read()).get("error", str(error))) from error

    def descriptor(self) -> dict:
        return {
            "name": f"open_{self.surface}_panel",
            "title": "计算作业监控" if self.surface == "monitor" else "研究方案",
            "description": "Open the research workspace beside the conversation.",
            "inputSchema": {
                "type": "object",
                "properties": {"workspace_id": {"type": "string", "format": "uuid"}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "_meta": {
                "ui": {"resourceUri": self.uri, "visibility": ["app", "model"]},
                "openai/ui": {"entrypoints": [{"type": "thread"}]},
            },
        }

    def dispatch(self, method: str, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("MCP params must be an object")
        if method == "initialize":
            version = params.get("protocolVersion")
            return {
                "protocolVersion": version
                if version in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                else "2025-11-25",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": f"chem-{self.surface}", "version": "0.1.0"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [self.descriptor(), *self.request(f"/catalog/{self.surface}")["tools"]]
            }
        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": self.uri,
                        "name": "Research panel",
                        "mimeType": "text/html;profile=mcp-app",
                    }
                ]
            }
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            if params.get("uri") != self.uri:
                raise ValueError("未知的界面资源")
            return {
                "contents": [
                    {
                        "uri": self.uri,
                        "mimeType": "text/html;profile=mcp-app",
                        "text": (self.root / "panel.html").read_text(),
                        "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}},
                    }
                ]
            }
        if method == "tools/call":
            name, arguments = params["name"], params.get("arguments", {})
            try:
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be an object")
                if name == self.descriptor()["name"]:
                    data = {
                        "surface": self.surface,
                        "workspace_id": arguments.get("workspace_id"),
                        "automatic_wakeup": "unavailable",
                    }
                else:
                    allowed = {
                        tool["name"] for tool in self.request(f"/catalog/{self.surface}")["tools"]
                    }
                    if name not in allowed:
                        raise ValueError("此插件不提供该工具")
                    data = self.request(f"/tools/{name}", arguments)
                return {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                    "structuredContent": data,
                }
            except (ValueError, OSError) as error:
                return {"isError": True, "content": [{"type": "text", "text": str(error)}]}
        raise LookupError(f"Unsupported MCP method: {method}")


def main() -> None:
    relay = Relay(Path(__file__).resolve().parent)
    for line in sys.stdin:
        identifier = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC request must be an object")
            if "id" not in message:
                continue
            identifier = message["id"]
            result = relay.dispatch(message["method"], message.get("params", {}))
            response = {"jsonrpc": "2.0", "id": identifier, "result": result}
        except (KeyError, ValueError, OSError, LookupError) as error:
            response = {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {
                    "code": (
                        -32700
                        if isinstance(error, json.JSONDecodeError)
                        else -32601
                        if isinstance(error, LookupError)
                        else -32603
                    ),
                    "message": str(error),
                },
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
