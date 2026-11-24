"""Run the private loopback service owned by the macOS application."""

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from threading import Thread

import uvicorn
from pydantic_settings import PydanticBaseSettingsSource
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from chemistry_agent.api import create_app
from chemistry_agent.backends.service import BackendRouter
from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ModelScope, ProviderRouter, current_model_scope

HOST = "127.0.0.1"
COOKIE = "chem_code_desktop"
TOKEN_HEADER = "x-chem-code-launch"


class DesktopSettings(Settings):
    """Only explicit desktop settings can decide what workspace and credentials to use."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[Settings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def workspace_settings(root: Path, port: int) -> DesktopSettings:
    root = root.resolve()
    if any(part.casefold() == "archive" for part in root.parts):
        raise ValueError("Choose a private workspace outside the archive directory.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Chem Code requires a workspace directory private to this macOS account.")
    return DesktopSettings(
        _env_file=None,
        host=HOST,
        port=port,
        database_path=root / "agent.sqlite3",
        platform_path=root / "platform",
        slurm_history_path=root / "slurm-history.sqlite3",
        tokenizer_path=root / "deepseek-tokenizer.json",
        auth_enabled=False,
        automation_enabled=True,
        context_window_tokens=131072,
    )


async def configure_local_agent(backends: BackendRouter, provider: ProviderRouter) -> None:
    """Prefer a ready local CLI on a new workspace, without sending any chat request."""
    backends.reuse_native_profile = False
    async with backends.store.lock:
        saved = await backends.store._one("SELECT 1 FROM agent_backend_settings WHERE id=1")
    if saved:
        return
    local_scope = current_model_scope.set(ModelScope(None))
    try:
        if await provider.ready():
            return
    finally:
        current_model_scope.reset(local_scope)
    configuration = await backends.settings()
    for name, adapter in (("codex", backends.codex), ("antigravity", backends.antigravity)):
        if not adapter.executable(configuration):
            continue
        try:
            async with asyncio.timeout(5):
                probe = await adapter.probe(configuration)
        except (AgentError, OSError, ValueError, TimeoutError):
            continue
        if probe.get("authenticated"):
            await backends.save_settings(configuration.model_copy(update={"default_backend": name}))
            return


class DesktopAccess:
    """Authenticate HTTP and WebSocket requests before they reach the local workspace."""

    def __init__(
        self, app: ASGIApp, token: str, port: int, mcp_token_file: Path | None = None
    ) -> None:
        self.app = app
        self.token = token.encode("ascii")
        self.host = f"{HOST}:{port}"
        self.origin = f"http://{self.host}"
        self.mcp_token_file = mcp_token_file
        self.mcp_token = mcp_token_file.read_bytes().strip() if mcp_token_file else b""

    def matches(self, value: str) -> bool:
        return compare_digest(self.token, value.encode("utf-8"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        same_site = (
            headers.get("host") == self.host
            and origin in {None, self.origin}
            and headers.get("sec-fetch-site") not in {"cross-site", "same-site"}
        )
        if scope["type"] == "websocket":
            if same_site and origin == self.origin and self.cookie_matches(headers):
                await self.app(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return

        if same_site and scope["method"] == "GET" and scope["path"].startswith("/__desktop/"):
            if self.matches(headers.get(TOKEN_HEADER, "")):
                if scope["path"] == "/__desktop/ready":
                    await PlainTextResponse("ready", headers={"Cache-Control": "no-store"})(
                        scope, receive, send
                    )
                    return
                if scope["path"] == "/__desktop/start":
                    response = RedirectResponse(
                        "/",
                        status_code=303,
                        headers={
                            "Cache-Control": "no-store",
                        },
                    )
                    response.set_cookie(
                        COOKIE, self.token.decode("ascii"), httponly=True, samesite="strict"
                    )
                    await response(scope, receive, send)
                    return
        elif same_site and self.mcp_matches(scope, headers):
            await self.app(scope, receive, send)
            return
        elif same_site and self.cookie_matches(headers):
            if scope["path"] == "/api/knowledge/connection" and self.mcp_token_file:
                scope.setdefault("state", {})["desktop_mcp_token_file"] = str(self.mcp_token_file)
            await self.app(scope, receive, send)
            return
        await PlainTextResponse("Chem Code desktop session required.", status_code=403)(
            scope, receive, send
        )

    def cookie_matches(self, headers: Headers) -> bool:
        cookies = cookie_parser(headers.get("cookie", ""))
        return self.matches(cookies.get(COOKIE, ""))

    def mcp_matches(self, scope: Scope, headers: Headers) -> bool:
        return (
            bool(self.mcp_token)
            and scope["method"] == "POST"
            and scope["path"] == "/api/knowledge/mcp"
            and compare_digest(
                b"Bearer " + self.mcp_token, headers.get("authorization", "").encode("utf-8")
            )
        )


def main() -> None:
    os.umask(0o077)
    root = Path(os.environ["CHEM_CODE_DESKTOP_DATA"]).resolve()
    os.environ.pop("CODEX_HOME", None)
    token = os.environ.pop("CHEM_CODE_DESKTOP_TOKEN")
    port = int(os.environ["CHEM_CODE_DESKTOP_PORT"])
    ready_file = Path(os.environ.pop("CHEM_CODE_DESKTOP_READY_FILE"))
    settings = workspace_settings(root, port)

    # The native parent holds this pipe open. A crash or Force Quit closes it.
    def follow_parent() -> None:
        while os.read(sys.stdin.fileno(), 1024):
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    with socket.create_server((HOST, port)) as server_socket:
        mcp_token_file = root / "knowledge-mcp.token"
        if not mcp_token_file.exists():
            mcp_token_file.write_text(token_urlsafe(32) + "\n")
        config = uvicorn.Config(
            DesktopAccess(
                create_app(settings, prepare_local=configure_local_agent),
                token,
                port,
                mcp_token_file,
            ),
            host=HOST,
            port=port,
            proxy_headers=False,
            access_log=False,
            timeout_graceful_shutdown=8,
        )
        # Let the native parent confirm which process owns the port before sending its token.
        ready_file.touch(exist_ok=False)
        Thread(target=follow_parent, daemon=True).start()
        asyncio.run(uvicorn.Server(config).serve(sockets=[server_socket]))


if __name__ == "__main__":
    main()
