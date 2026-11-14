"""ACP v1 JSON-RPC transport, independent of any particular Agent vendor."""

import asyncio
import json

from chemistry_agent.backends.runtime import ManagedTransport
from chemistry_agent.errors import AgentError


class ACPTransport(ManagedTransport):
    def __init__(self, executable: str, arguments: list[str], cwd: str) -> None:
        self.executable, self.arguments, self.cwd = executable, arguments, cwd
        self.process = None
        self.reader = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events = asyncio.Queue()
        self.sequence = 0
        self.initialized: dict = {}

    async def start(self):
        await self.spawn(self.executable, *self.arguments, cwd=self.cwd)
        self.reader = asyncio.create_task(self.read())
        self.initialized = await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "chem-code", "title": "Chem Code", "version": "0.1.0"},
                # The native Agent owns filesystem/terminal tools; no unimplemented client RPCs.
                "clientCapabilities": {},
            },
        )
        if self.initialized.get("protocolVersion") != 1:
            raise AgentError("acp_version", "此 Agent 的 ACP 版本暂不支持。", 502)
        return self

    async def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float | None = 45) -> dict:  # noqa: ASYNC109
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({"id": identifier, "method": method, "params": params})
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self.pending.pop(identifier, None)

    async def read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                future = self.pending.get(message.get("id")) if "method" not in message else None
                if future is None:
                    await self.events.put(message)
                elif not future.done():
                    if "error" in message:
                        error = message["error"]
                        code = "agent_auth_required" if error.get("code") == -32000 else "acp_error"
                        future.set_exception(
                            AgentError(code, str(error.get("message"))[:1000], 502)
                        )
                    else:
                        future.set_result(message.get("result", {}))
        except (ValueError, OSError):
            pass
        finally:
            error = AgentError("acp_disconnected", "本地 Agent 连接中断，未自动重发消息。", 502)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            await self.events.put({"transport_error": "disconnected"})
