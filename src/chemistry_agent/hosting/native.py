"""One remote lifetime for chat, resume, model discovery and native compaction."""

import asyncio
import inspect
import json
import stat
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.connection import connection, helper, helper_command

current_native: ContextVar["RemoteNative | None"] = ContextVar("remote_native", default=None)


class HostedBackend:
    """Enter SSH only after the delivery ledger has admitted this exact input."""

    def __init__(self, router, adapter, config, backend: str) -> None:
        self.router, self.adapter, self.config, self.backend = router, adapter, config, backend

    async def run(self, messages: list[dict], progress, **kwargs) -> dict:
        from chemistry_agent.backends.runtime import delivery_attempt

        with delivery_attempt(progress):
            async with native_scope(self.router, self.config, kwargs["session_id"], self.backend):
                baseline = await self.router.output_snapshot(kwargs["session_id"])
                try:
                    return await self.adapter.run(messages, progress, **kwargs)
                finally:
                    await self.router.publish_workspace(
                        kwargs["session_id"], kwargs.get("run_id"), baseline
                    )


class RemoteNative:
    def __init__(self, router, config, session: str, backend: str) -> None:
        self.router, self.config, self.session, self.backend = router, config, session, backend
        namespace = router.hosting.installation()
        base = f"~/.local/state/chem-code/{namespace}"
        self.home = f"{base}/agents/{config.instance_id or backend}/{session}"
        self.workspace = chosen_directory(config, session) or f"{base}/workspaces/{session}"
        self.stack = AsyncExitStack()
        self.forward = None

    async def __aenter__(self):
        try:
            self.ssh = await self.stack.enter_async_context(
                connection(self.router.hosting, self.config.host_id)
            )
            info = await helper(self.ssh, self.payload("prepare"))
            self.home, self.workspace, self.python = info["home"], info["workspace"], info["python"]
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    def payload(self, operation: str) -> dict:
        return {
            "operation": operation,
            "home": self.home,
            "workspace": self.workspace,
            "backend": self.backend,
            "history_name": history_name(self.config, self.session),
        }

    async def start(self, arguments: tuple[str, ...]):
        # ACP private-home flags are added on the remote host, never with Mac paths.
        args = list(arguments[1:])
        if self.backend == "antigravity" and "--gemini-dir" in args:
            args = args[: args.index("--gemini-dir")]
        process = await self.ssh.create_process(helper_command(), encoding=None)
        process.stdin.write(
            (
                json.dumps(
                    {**self.payload("native"), "executable": arguments[0], "arguments": args}
                )
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
        try:
            async with asyncio.timeout(30):
                response = json.loads(await process.stdout.readline())
            if not response.get("ready"):
                raise AgentError(
                    "agent_remote_start", response.get("error", "远端 Agent 未启动。"), 502
                )
        except BaseException:
            process.close()
            await process.wait_closed()
            raise
        return process

    async def history(self, messages: list[dict]) -> None:
        await helper(self.ssh, {**self.payload("prepare"), "history": messages})

    async def materialize_skill(self, identifier: str, source: Path) -> str:
        from chemistry_agent.knowledge.plugins import files

        target = Path(self.workspace) / ".chem-skills" / identifier
        async with self.ssh.start_sftp_client() as sftp:
            await sftp.makedirs(str(target), exist_ok=True)
            actual_root = Path(await sftp.realpath(self.workspace))
            for path in await asyncio.to_thread(lambda: list(files(source))):
                destination = target / path.relative_to(source)
                await sftp.makedirs(str(destination.parent), exist_ok=True)
                actual_parent = Path(await sftp.realpath(str(destination.parent)))
                if not actual_parent.is_relative_to(actual_root):
                    raise AgentError("skill_path_invalid", "远端 Skill 目录包含外部链接。", 422)
                if await sftp.lexists(str(destination)):
                    continue  # Preserve the remote Agent's existing working edits.
                await sftp.put(str(path), str(destination), preserve=True, follow_symlinks=False)
        return str(target)

    async def bridge(self, bridge) -> dict:
        from chemistry_agent.backends.mcp_bridge import main

        # SSH stream-local forwarding is turn-scoped, authenticated, and never public TCP.
        remote_socket = f"/tmp/chem-{uuid4().hex}.sock"
        self.forward = await self.ssh.forward_remote_path(remote_socket, str(bridge.path))
        self.stack.push_async_callback(self.close_forward, remote_socket)
        code = "import os,json,socket,sys\n" + inspect.getsource(main) + "\nmain()\n"
        return {
            "name": "chem_platform",
            "command": self.python,
            "args": ["-u", "-c", code],
            "env": [
                {"name": "CHEM_MCP_SOCKET", "value": remote_socket},
                {"name": "CHEM_MCP_TOKEN", "value": bridge.token},
            ],
        }

    async def close_forward(self, path: str) -> None:
        if self.forward:
            self.forward.close()
            await self.forward.wait_closed()
        try:
            async with self.ssh.start_sftp_client() as sftp:
                await sftp.remove(path)
        except Exception:
            pass  # Socket credentials expire with the turn-scoped bridge.

    async def sync_outputs(self, paths: list[str]) -> list[str]:
        from chemistry_agent.workspace_files import MAX_FILE_BYTES

        root = Path(self.workspace)
        self.router.workspace_files.origins[self.session] = (self.config.host_id, root)
        destination = self.router.root / self.session
        selected = []
        async with self.ssh.start_sftp_client() as sftp:
            requested = []
            for value in paths:
                path = Path(value)
                if path.is_absolute():
                    try:
                        path = path.relative_to(root)
                    except ValueError as exc:
                        raise AgentError(
                            "workspace_path_invalid", "请发布当前研究目录中的文件。", 422
                        ) from exc
                if any(p in {"..", "archive"} or p.startswith(".") for p in path.parts):
                    raise AgentError(
                        "workspace_path_invalid", "请发布当前研究目录中的成果文件。", 422
                    )
                requested.append(path.as_posix())
            for name in requested:
                path = Path(name)
                local = destination / path
                self.router.workspace_files.source(self.session, path.as_posix())
                import asyncssh

                try:
                    actual = Path(await sftp.realpath(str(root / path)))
                except asyncssh.SFTPError as exc:
                    raise AgentError(
                        "workspace_file_unavailable", f"远端文件不存在：{path}", 422
                    ) from exc
                if not actual.is_relative_to(root):
                    raise AgentError("workspace_path_invalid", "文件链接超出当前目录。", 422)
                try:
                    before = await sftp.lstat(str(root / path))
                except (OSError, asyncssh.SFTPError) as exc:
                    raise AgentError(
                        "workspace_file_unavailable", f"远端文件不存在：{path}", 422
                    ) from exc
                if not stat.S_ISREG(before.permissions or 0) or (before.size or 0) > MAX_FILE_BYTES:
                    continue
                local.parent.mkdir(parents=True, exist_ok=True)
                temporary = local.with_name(f".{local.name}.{uuid4().hex}.partial")
                try:
                    await sftp.get(str(root / path), str(temporary), follow_symlinks=False)
                    after = await sftp.stat(str(root / path))
                    if (before.size, before.mtime) != (after.size, after.mtime):
                        raise AgentError(
                            "workspace_file_changing", "远端文件仍在写入，请稍后发布。", 409
                        )
                    temporary.replace(local)
                finally:
                    temporary.unlink(missing_ok=True)
                selected.append(path.as_posix())
            missing = sorted(set(requested) - set(selected))
            if missing:
                raise AgentError(
                    "workspace_file_unavailable",
                    f"远端文件不存在或暂不可发布：{', '.join(missing[:3])}",
                    422,
                )
        return selected

    async def __aexit__(self, *args) -> None:
        await self.stack.aclose()


@asynccontextmanager
async def native_scope(router, config, session: str, backend: str):
    if config.host_id == "local":
        yield None
        return
    async with RemoteNative(router, config, session, backend) as remote:
        token = current_native.set(remote)
        try:
            yield remote
        finally:
            current_native.reset(token)


def chosen_directory(config, session: str | None) -> str:
    """Legacy spaces retain their exact directory without remaining a setting."""
    if config.working_directory:
        return config.working_directory
    if config.workspace_path and session:
        return f"{config.workspace_path.rstrip('/')}/{session}"
    return ""


def history_name(config, session: str) -> str:
    # Explicit folders may be shared by several conversations.
    return (
        f".chem-code/history-{session}.json"
        if config.working_directory
        else "platform-history.json"
    )


def workspace(router, config, session: str) -> Path:
    remote = current_native.get()
    if remote:
        return Path(remote.workspace)
    directory = chosen_directory(config, session)
    return (Path(directory).expanduser() if directory else router.root / session).resolve()


async def save_history(router, config, session: str, messages: list[dict]) -> Path:
    directory = workspace(router, config, session)
    remote = current_native.get()
    if remote:
        await remote.history(messages)
    else:
        directory.mkdir(parents=True, exist_ok=True)
        history = directory / history_name(config, session)
        if not history.resolve().is_relative_to(directory.resolve()):
            raise AgentError("history_path_invalid", "会话记录目录包含外部链接。", 422)
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(messages, ensure_ascii=False))
    return directory
