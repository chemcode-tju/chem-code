"""Codex app-server stdio transport. Never read or copy the user's auth tokens."""

import asyncio
import json
import os

from chemistry_agent.backends.executables import prepend_executable_dir
from chemistry_agent.backends.runtime import ManagedTransport
from chemistry_agent.errors import AgentError


class CodexTransport(ManagedTransport):
    def __init__(self, executable: str) -> None:
        self.executable = executable
        self.process = None
        self.reader = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events = asyncio.Queue()
        self.sequence = 0
        self.codex_home = None

    async def __aenter__(self):
        # Compatibility with callers that configure a home directly.
        if self.codex_home:
            self.runtime_home = self.codex_home
            self.runtime_backend = "codex"
        return await super().__aenter__()

    async def start(self):
        environment = prepend_executable_dir(os.environ.copy(), self.executable)
        if self.codex_home:
            environment["CODEX_HOME"] = str(self.codex_home)
        await self.spawn(self.executable, "app-server", "--listen", "stdio://", env=environment)
        self.reader = asyncio.create_task(self.read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "chem_code", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized"})
        return self

    async def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({"id": identifier, "method": method, "params": params})
            async with asyncio.timeout(30):
                return await future
        finally:
            self.pending.pop(identifier, None)

    async def read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" not in message and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            detail = str(message["error"].get("message", "Codex 请求失败"))[:1000]
                            future.set_exception(
                                AgentError(
                                    "codex_session_busy"
                                    if "active writer" in detail
                                    else "codex_protocol_error",
                                    "Codex 会话已被另一进程占用，尚未发送本轮任务。"
                                    if "active writer" in detail
                                    else detail,
                                    502,
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    await self.events.put(message)
        except (ValueError, OSError) as exc:
            await self.events.put({"transport_error": type(exc).__name__})
        finally:
            error = AgentError("codex_disconnected", "本地 Codex 连接已中断，未自动重发消息。", 502)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            await self.events.put({"transport_error": "disconnected"})
