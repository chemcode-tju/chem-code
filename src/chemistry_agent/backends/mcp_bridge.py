"""Turn-scoped MCP relay: native Agents use the host's existing tool registry."""

import asyncio
import hmac
import json
import os
import secrets
import socket
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path


class PlatformMCPBridge:
    """Bind every call to a host-owned session/run; never accept identity from the Agent."""

    def __init__(
        self, schemas: list[dict], execute: Callable[[str, dict], Awaitable[dict]],
        *, name: str = "chem_platform",
    ) -> None:
        self.name = name
        self.tools = [
            dict(
                name=s["function"]["name"],
                description=s["function"]["description"],
                inputSchema=s["function"]["parameters"],
            )
            for s in schemas
        ]
        self.execute = execute
        self.token = secrets.token_urlsafe(32)
        self.handlers: set[asyncio.Task] = set()

    async def __aenter__(self):
        # Short path also fits macOS's 104-byte Unix socket limit.
        self.directory = tempfile.TemporaryDirectory(prefix="chem-mcp-", dir="/tmp")
        self.path = Path(self.directory.name) / "bridge.sock"
        self.server = await asyncio.start_unix_server(
            self.accept, str(self.path), limit=16 * 1024 * 1024
        )
        self.path.chmod(0o600)
        return self

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "command": sys.executable,
            "args": ["-m", "chemistry_agent.backends.mcp_bridge"],
            "env": [
                {"name": name, "value": value}
                for name, value in {
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                    "CHEM_MCP_SOCKET": str(self.path),
                    "CHEM_MCP_TOKEN": self.token,
                }.items()
            ],
        }

    async def accept(self, reader, writer) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            async with asyncio.timeout(10):
                envelope = json.loads(await reader.readline())
            if not hmac.compare_digest(str(envelope.get("token", "")), self.token):
                raise ValueError("Invalid bridge token")
            message = envelope["message"]
            identifier, method = message.get("id"), message.get("method")
            result = {}
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "chem-platform", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {"tools": self.tools}
            elif method == "tools/call":
                params = message["params"]
                result = await self.execute(params["name"], params.get("arguments", {}))
            elif method != "ping":
                writer.write(
                    (
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": identifier,
                                "error": {"code": -32601, "message": "Unsupported MCP method"},
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                return
            writer.write(
                (json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}) + "\n").encode()
            )
            await writer.drain()
        except (ValueError, KeyError, OSError, TimeoutError):
            # Invalid/expired credentials never reach the registry.
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def __aexit__(self, *args) -> None:
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.directory.cleanup()


def main() -> None:
    """Only the native subprocess speaks MCP stdio; stdout contains no application logs."""
    path, token = os.environ["CHEM_MCP_SOCKET"], os.environ["CHEM_MCP_TOKEN"]
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:  # MCP notifications have no response.
            continue
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(path)
                connection.sendall(
                    (json.dumps({"token": token, "message": message}) + "\n").encode()
                )
                with connection.makefile("rb") as stream:
                    response = stream.readline()
            if not response:
                raise OSError("Platform bridge closed")
            sys.stdout.buffer.write(response)
            sys.stdout.buffer.flush()
        except OSError:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32603,
                            "message": "Platform turn ended; do not replay this tool call.",
                        },
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
