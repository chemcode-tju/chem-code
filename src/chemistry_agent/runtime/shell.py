"""Noninteractive SSH for one explicitly selected host and one user turn."""

import asyncio
import re
from contextvars import ContextVar
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from chemistry_agent.automation.local_ssh import enabled_aliases
from chemistry_agent.automation.ssh_profiles import load_profile, shell_profiles
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext

SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
shell_grant: ContextVar[str | None] = ContextVar("shell_grant", default=None)
OUTPUT_BYTES = 16_384


class ShellInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1, max_length=16_384)
    host: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    request_id: str | None = Field(
        default=None,
        description="Runner command receipt UUID; reuse only to reconcile the same operation",
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    )


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


async def profiled_command(root: Path, identifier: str, host: str, command: str) -> dict:
    from chemistry_agent.automation.ssh_connection import connect

    profile = load_profile(root, identifier)
    async with await connect(root, profile) as connection:
        async with connection.create_process("sh -s", encoding=None) as process:
            process.stdin.write((command + "\n").encode())
            process.stdin.write_eof()
            stdout, stderr = await asyncio.gather(
                capped_read(process.stdout), capped_read(process.stderr)
            )
            await process.wait_closed()
            return {
                "host": host,
                "exit_code": process.returncode,
                "stdout": stdout[0],
                "stderr": stderr[0],
                "truncated": stdout[1] or stderr[1],
                "ok": process.returncode == 0,
            }


def remote_shell_tool(hosts: tuple[str, ...], root: Path | None = None) -> Tool:
    async def execute(args: ShellInput, context: ToolContext) -> dict:
        # The local settings selection is a durable grant, shared by new conversations.
        available = enabled_aliases(root, hosts) if root else hosts
        host = args.host or shell_grant.get()
        if root and host:
            from chemistry_agent.environments.registry import Environments

            environments = Environments(root)
            if environments.get(host):
                # A native tool replay keeps its identity even if the HTTP response was lost.
                receipt = (
                    uuid5(
                        NAMESPACE_URL,
                        f"chem-code:{context.session_id}:{context.run_id}:{context.tool_call_id}",
                    )
                    if context.tool_call_id
                    else uuid4()
                )
                identifier = str(UUID(args.request_id)) if args.request_id else str(receipt)
                try:
                    return {
                        "host": host,
                        **await environments.request(
                            host,
                            "shell",
                            {
                                "id": identifier,
                                "command": args.command,
                            },
                        ),
                    }
                except AgentError as exc:
                    return {
                        "host": host,
                        "id": identifier,
                        "state": "unknown",
                        "error": exc.message,
                        "next": "Inspect Runner receipt or scheduler; do not resubmit blindly",
                    }
        profiles = shell_profiles(root) if root and host else {}
        if host in profiles:
            try:
                async with asyncio.timeout(30):
                    return await profiled_command(root, profiles[host], host, args.command)
            except TimeoutError as exc:
                raise AgentError(
                    "ssh_timeout", "SSH 已超时，远端操作状态未知；请先检查结果。", 504
                ) from exc
        if not host or host not in available or (root is None and host != shell_grant.get()):
            raise AgentError(
                "ssh_authorization_required", "请在服务器设置中启用该 SSH 别名，并指定 host。", 403
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
        "Run a shell command on a locally enabled server using host. "
        "Use calculation_job_hosts for the current list. Inspect shell/config files and "
        "discover software and potential libraries yourself before asking the user for paths. "
        "Do not read private key contents. Configured hosts: "
        + ", ".join(hosts)
        + ". After sbatch, register the job with calculation_job_adopt for durable monitoring.",
        ShellInput,
        execute,
        contextual=True,
    )
