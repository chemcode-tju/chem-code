from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from chemistry_agent.api import create_app
from chemistry_agent.backends.antigravity_home import AntigravityHomes
from chemistry_agent.backends.codex import CodexBackend
from chemistry_agent.backends.codex_home import CodexHomes
from chemistry_agent.desktop import (
    COOKIE,
    TOKEN_HEADER,
    DesktopAccess,
    configure_local_agent,
    workspace_settings,
)


def test_desktop_bootstrap_protects_http_and_websockets(tmp_path: Path) -> None:
    app = FastAPI()

    @app.get("/")
    def index() -> dict:
        return {"ok": True}

    @app.post("/api/knowledge/mcp")
    def knowledge() -> dict:
        return {"mcp": True}

    @app.websocket("/stream")
    async def stream(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ok")

    origin = "http://127.0.0.1:17895"
    socket_url = "ws://127.0.0.1:17895/stream"
    mcp_file = tmp_path / "knowledge.token"
    mcp_file.write_text("mcp-token\n")
    with TestClient(DesktopAccess(app, "test-token", 17895, mcp_file), base_url=origin) as client:
        assert client.get("/").status_code == 403
        bearer = {"Authorization": "Bearer mcp-token"}
        assert client.get("/", headers=bearer).status_code == 403
        assert client.post("/api/knowledge/mcp").status_code == 403
        assert client.post("/api/knowledge/mcp", headers=bearer).json() == {"mcp": True}
        assert client.get("/__desktop/start").status_code == 403
        assert client.get("/__desktop/ready", headers={TOKEN_HEADER: "test-token"}).text == "ready"
        bootstrap = client.get(
            "/__desktop/start", headers={TOKEN_HEADER: "test-token"}, follow_redirects=False
        )
        assert bootstrap.status_code == 303
        cookie = bootstrap.headers["set-cookie"]
        assert COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert client.get("/").json() == {"ok": True}
        assert client.get("/", headers={"Host": "other.invalid"}).status_code == 403
        assert client.get("/", headers={"Origin": "http://localhost:8000"}).status_code == 403
        assert client.get("/", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(socket_url, headers={"Origin": "https://other.invalid"}),
        ):
            pass
        with client.websocket_connect(socket_url, headers={"Origin": origin}) as websocket:
            assert websocket.receive_text() == "ok"


def test_desktop_workspace_ignores_server_paths_and_model_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_database = tmp_path / "research" / "existing.sqlite3"
    monkeypatch.setenv("CHEMISTRY_DATABASE_PATH", str(old_database))
    monkeypatch.setenv("CHEMISTRY_PLATFORM_PATH", str(tmp_path / "research"))
    monkeypatch.setenv("CHEMISTRY_SSH_HOSTS", "old-workspace")
    monkeypatch.setenv("CHEMISTRY_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-test-key")
    root = tmp_path / "新 工作区"
    settings = workspace_settings(root, 17895)
    assert settings.database_path == root / "agent.sqlite3"
    assert settings.platform_path == root / "platform"
    assert not settings.auth_enabled and not settings.model_configured
    assert not settings.allowed_ssh_hosts and not old_database.exists()
    assert root.stat().st_mode & 0o777 == 0o700
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        workspace_settings(directory, 17895)


def test_desktop_detects_a_ready_agent_and_retains_user_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(CodexBackend, "executable", staticmethod(lambda _: "/fixture/codex"))
    probe = AsyncMock(return_value={"authenticated": True})
    monkeypatch.setattr(CodexBackend, "probe", probe)
    settings = workspace_settings(tmp_path / "desktop", 17895)

    def no_provider(_: httpx.Request) -> httpx.Response:
        pytest.fail("First-run detection must not call a model.")

    for expected in ("codex", "builtin"):
        app = create_app(
            settings,
            transport=httpx.MockTransport(no_provider),
            prepare_local=configure_local_agent,
        )
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50100)) as client:
            assert client.get("/auth/status").json()["enabled"] is False
            assert client.get("/api/agents/selection").json()["backend"] == expected
            assert app.state.agent_backends.reuse_native_profile is False
            if expected == "codex":
                result = client.patch("/api/agents/settings/builtin", json={"make_default": True})
                assert result.is_success
    probe.assert_awaited_once()


def test_native_desktop_profiles_preserve_auth_without_importing_customizations(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "native"
    previous.mkdir()
    for filename in ("auth.json", "config.toml", "hooks.json", "settings.json"):
        (previous / filename).write_text("fixture")
    codex = CodexHomes(tmp_path / "codex", previous, reuse_profile=False).prepare("probe")
    assert {path.name for path in codex.iterdir()} == {"auth.json"}
    assert (codex / "auth.json").is_symlink()
    antigravity = tmp_path / "antigravity"
    AntigravityHomes(previous, reuse_profile=False).prepare(antigravity)
    assert not list(antigravity.iterdir())
