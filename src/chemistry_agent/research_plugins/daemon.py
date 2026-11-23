"""Loopback-only daemon. The stdio plugins relay authenticated requests here."""

import asyncio
import hmac
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from filelock import FileLock
from pydantic import ValidationError

from chemistry_agent.errors import AgentError
from chemistry_agent.research_plugins.app_server_client import AppServerClient
from chemistry_agent.research_plugins.dispatcher import Dispatcher
from chemistry_agent.research_plugins.observer import observe_slurm
from chemistry_agent.research_plugins.service import ResearchService

logger = logging.getLogger(__name__)


def create_app(
    home: Path,
    ssh_hosts: tuple[str, ...] = (),
    project_roots: tuple[Path, ...] = (),
    local_host: str = "",
    codex_socket: Path | None = None,
) -> FastAPI:
    home = home.expanduser().resolve()
    if "archive" in home.parts:
        raise ValueError("不能使用 archive 目录")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path = home / "access-token"
    try:
        with token_path.open("x") as stream:
            token_path.chmod(0o600)
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    token = token_path.read_text().strip()
    if len(token) < 32:
        raise ValueError("监控服务 access-token 无效")
    service = ResearchService(home, ssh_hosts, project_roots, local_host, codex_socket is not None)

    async def simulations() -> None:
        while True:
            service.ledger.tick_simulations()
            await asyncio.sleep(0.5)

    async def slurm() -> None:
        while True:
            await observe_slurm(service.ledger, ssh_hosts, local_host)
            await asyncio.sleep(15)

    async def dispatch(socket: Path, stop: asyncio.Event) -> None:
        dispatcher = Dispatcher(service.ledger, lambda: AppServerClient(socket))
        try:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(dispatcher.tick)
                except Exception:
                    logger.exception("无法处理完成事件")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)
        finally:
            await asyncio.to_thread(dispatcher.close)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with FileLock(str(home / "daemon.lock"), timeout=0):
            tasks = [asyncio.create_task(simulations()), asyncio.create_task(slurm())]
            stop = asyncio.Event()
            delivery_task = (
                asyncio.create_task(dispatch(codex_socket, stop)) if codex_socket else None
            )
            try:
                yield
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task
                if delivery_task:
                    await delivery_task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.research = service

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        # No browser-origin requests or cookies: MCP clients use a private bearer file.
        supplied = request.headers.get("authorization", "")
        if request.headers.get("origin") or not hmac.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "automatic_wakeup": "enabled" if codex_socket else "unavailable",
            "delivery": "codex" if codex_socket else "pull",
            "model_runtime": False,
            "ssh_hosts": list(ssh_hosts),
            "local_host": local_host,
        }

    @app.get("/catalog/{surface}")
    async def catalog(surface: str) -> dict:
        if surface not in {"monitor", "research"}:
            return {"tools": []}
        return {"tools": service.catalog(surface)}

    @app.post("/tools/{name}")
    async def execute(name: str, request: Request) -> JSONResponse:
        if len(await request.body()) > 2 * 1024 * 1024:
            return JSONResponse({"error": "请求超过 2 MiB"}, status_code=413)
        try:
            data = await service.execute(name, await request.json())
            return JSONResponse(data)
        except (ValueError, ValidationError, AgentError) as error:
            return JSONResponse({"error": str(error)}, status_code=422)

    return app
