"""Noninteractive SSH for one explicitly selected host and one user turn."""

import asyncio
import re
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, Field

from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext

SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
shell_grant: ContextVar[str | None] = ContextVar("shell_grant", default=None)
OUTPUT_BYTES = 16_384


class ShellInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1, max_length=16_384)


def authorized_host(message: str, hosts: tuple[str, ...]) -> str | None:
    header = message.strip().partition("\n")[0]
    match = re.fullmatch(r"/shell[ \t]+(\S+)", header)
    if not match:
        return None
    if match[1] not in hosts:
        raise AgentError("ssh_host_denied", "服务器不在已配置的 SSH 别名列表中。", 403)
    return match[1]


async def capped_read(reader: asyncio.StreamReader) -> tuple[str, bool]:
    buffer = bytearray()
    truncated = False
    while chunk := await reader.read(8192):
        room = OUTPUT_BYTES - len(buffer)
        buffer.extend(chunk[:room])
        truncated |= len(chunk) > room
    return buffer.decode("utf-8", errors="replace"), truncated


def remote_shell_tool(hosts: tuple[str, ...]) -> Tool:
    async def execute(args: ShellInput, context: ToolContext) -> dict:
        host = shell_grant.get()
        if not host or host not in hosts:
            raise AgentError(
                "ssh_authorization_required", "请在本轮消息首行输入 /shell 服务器别名。", 403
            )
        # No local shell, no inherited forwards, no interactive/password fallback.
        command = [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=8",
            "-oForwardAgent=no",
            "-oForwardX11=no",
            "-oClearAllForwardings=yes",
            "-oPermitLocalCommand=no",
            "-oControlMaster=no",
            "-oControlPath=none",
            "--",
            host,
            "sh -s",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AgentError("ssh_unavailable", "无法启动系统 SSH 客户端。", 503) from exc

        async def feed() -> None:
            proc.stdin.write((args.command + "\n").encode())
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        try:
            async with asyncio.timeout(30):
                _, stdout, stderr, code = await asyncio.gather(
                    feed(), capped_read(proc.stdout), capped_read(proc.stderr), proc.wait()
                )
            return {
                "host": host,
                "exit_code": code,
                "stdout": stdout[0],
                "stderr": stderr[0],
                "truncated": stdout[1] or stderr[1],
                "ok": code == 0,
            }
        except TimeoutError as exc:
            raise AgentError(
                "ssh_timeout", "SSH 已超时，远端操作状态未知；请检查结果后再决定是否重试。", 504
            ) from exc
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    return Tool(
        "remote_shell",
        "Run a non-interactive shell command on the server selected by "
        "the user's /shell HOST header. Configured hosts: "
        + ", ".join(hosts)
        + ". For VASP submissions use calculation tools.",
        ShellInput,
        execute,
        contextual=True,
    )
