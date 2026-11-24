"""Interactive SSH PTY for the local single-user web workspace."""

import asyncio
import json
import shlex
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.responses import FileResponse

from chemistry_agent.automation.routes import LOCAL_HOSTS, local_service
from chemistry_agent.backends.service import BackendSettings
from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.connection import connection
from chemistry_agent.hosting.native import RemoteNative, chosen_directory, workspace
from chemistry_agent.hosting.registry import validate_directory

router = APIRouter()


async def location(app, session: str) -> dict:
    backends = app.state.agent_backends
    binding = await backends.binding(session)
    config = BackendSettings.model_validate_json(binding["config"])
    host = backends.hosting.host(config.host_id)
    directory = chosen_directory(config, session)
    if not directory:
        if host["id"] == "local":
            directory = str(workspace(backends, config, session))
        else:
            directory = RemoteNative(backends, config, session, binding["backend"]).workspace
    hosts = {
        row["id"]: row
        for row in backends.hosting.hosts()
        if row["enabled"] and row["transport"] == "ssh"
    }
    targets = []

    def add(identifier: str, path: str, label: str) -> None:
        if identifier not in hosts or not path:
            return
        try:
            validate_directory(path)
        except ValueError:
            return
        if not any(row["host"] == identifier and row["directory"] == path for row in targets):
            targets.append({"host": identifier, "directory": path, "label": label})

    add(host["id"], directory, host["name"] + " · " + directory)
    chat = app.state.calculation_chat
    if (chat.root / session / "slurm_jobs").is_dir():
        for job in await asyncio.to_thread(chat.list_records, session, "slurm_jobs"):
            job_host = job.get("host", "")
            path = job.get("remote_directory", "")
            number = job.get("scheduler_job_id") or job.get("name") or path
            add(job_host, path, f"{job_host} · {number}")
    for remote in hosts.values():
        if not any(row["host"] == remote["id"] for row in targets):
            add(remote["id"], "~/", remote["name"] + " · ~/")
    return {
        "targets": targets,
        "host": host["id"],
        "name": host["name"],
        "directory": directory,
        "terminal": host["transport"] == "ssh",
    }


@router.get("/api/hosting/sessions/{session_id}/location")
async def session_location(session_id: UUID, request: Request) -> dict:
    local_service(request)
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    return await location(request.app, str(session_id))


@router.get("/terminal/{session_id}", include_in_schema=False)
async def page(session_id: UUID, request: Request) -> FileResponse:
    local_service(request)
    return FileResponse(Path(__file__).parents[1] / "web" / "terminal.html")


async def relay(socket: WebSocket, proc) -> None:
    async def output() -> None:
        while data := await proc.stdout.read(16384):
            await socket.send_bytes(data)

    async def keyboard() -> None:
        async for raw in socket.iter_text():
            message = json.loads(raw)
            if message.get("type") == "input":
                value = message["data"]
                if not isinstance(value, str) or len(value) > 65536:
                    raise ValueError("invalid terminal input")
                proc.stdin.write(value.encode())
                await proc.stdin.drain()
            elif message.get("type") == "resize":
                cols, rows = int(message["cols"]), int(message["rows"])
                if 2 <= cols <= 1000 and 1 <= rows <= 500:
                    proc.change_terminal_size(cols, rows)

    tasks = {asyncio.create_task(output()), asyncio.create_task(keyboard())}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/api/hosting/sessions/{session_id}/terminal")
async def terminal(socket: WebSocket, session_id: UUID, host: str, directory: str) -> None:
    # HTTP middleware does not authenticate WebSocket upgrades. Enforce the
    # same loopback-only boundary plus a mandatory exact browser origin.
    url = socket.url
    origin = f"{'https' if url.scheme == 'wss' else 'http'}://{url.netloc}"
    if (
        socket.app.state.settings.auth_enabled
        or url.hostname not in LOCAL_HOSTS
        or not socket.client
        or socket.client.host not in LOCAL_HOSTS
        or socket.headers.get("origin") != origin
    ):
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        store = socket.app.state.store
        async with store.lock:
            row = await store._one(
                "SELECT 1 FROM sessions WHERE id=? AND owner_id IS NULL", (str(session_id),)
            )
        if not row:
            raise AgentError("session_not_found", "会话不存在。", 404)
        actual = await location(socket.app, str(session_id))
        if not any(
            (host, directory) == (row["host"], row["directory"]) for row in actual["targets"]
        ):
            raise AgentError("terminal_moved", "主机或目录已更改，请从工作区重新打开。", 409)
        path = validate_directory(directory)
        quoted = '"$HOME"/' + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)
        command = f'cd -- {quoted} && exec "${{SHELL:-/bin/sh}}" -l'
        async with connection(socket.app.state.agent_backends.hosting, host) as ssh:
            async with ssh.create_process(
                command, term_type="xterm-256color", encoding=None
            ) as proc:
                await socket.send_json({"status": "connected"})
                await relay(socket, proc)
    except WebSocketDisconnect:
        return
    except (AgentError, ValueError, OSError, TimeoutError) as exc:
        await socket.send_json({"error": str(exc)})
    finally:
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect, OSError):
            pass
