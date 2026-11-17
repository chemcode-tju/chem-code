import os
import sys

import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.automation.modeling import execute_model
from chemistry_agent.errors import AgentError


@pytest.mark.parametrize("origin", ["http://evil.example", None])
def test_chat_calculation_local_boundary(settings_factory, origin):
    app = create_app(settings_factory(automation_enabled=True))
    with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 5000)) as client:
        response = client.post(
            "/chat", json={"message": "提交"}, headers={"Origin": origin} if origin else {}
        )
        assert response.status_code == 403


def test_shared_mode_has_no_calculation_tools(settings_factory):
    app = create_app(settings_factory(automation_enabled=True, auth_enabled=True))
    # Inspect registration without bypassing the shared authentication middleware.
    with TestClient(app) as client:
        assert set(app.state.agent.registry.tools) == {
            "molecular_properties",
            "molecular_similarity",
            "web_search",
            "ask_user_question",
            "memory_search",
            "memory_read",
            "memory_save",
            "memory_compare",
            "workspace_publish",
        }
        assert client.get("/api/automation/status").status_code in {401, 403}


@pytest.mark.skipif(
    sys.platform != "darwin" or os.getenv("CHEM_SANDBOX_TEST") != "1",
    reason="Requires explicit macOS sandbox integration run outside nested sandbox",
)
@pytest.mark.asyncio
async def test_real_ase_sandbox_blocks_files_network_and_subprocess(tmp_path):
    directory = (tmp_path / "model").resolve()
    directory.mkdir()
    secret = tmp_path / "private-sentinel"
    secret.write_text("not-readable")
    code = f"""
from pathlib import Path
import socket
import subprocess
from ase.build import bulk
from ase.io import write

def main():
    for operation in [lambda: Path({str(secret)!r}).read_text(),
                      lambda: socket.create_connection(("127.0.0.1", 9)),
                      lambda: subprocess.run(["/usr/bin/true"], check=True)]:
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise RuntimeError("Sandbox boundary failed")
    assert "DEEPSEEK_API_KEY" not in __import__("os").environ
    write("structure.extxyz", bulk("Cu", "fcc", a=3.6))

if __name__ == "__main__":
    main()
"""
    content = await execute_model(directory, code)
    assert "Cu" in content
    assert (directory / "model.py").read_text() == code


@pytest.mark.asyncio
async def test_model_execution_fails_closed_without_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr("chemistry_agent.automation.modeling.sys.platform", "linux")
    with pytest.raises(AgentError, match="受限建模执行器"):
        await execute_model(tmp_path, "raise RuntimeError('must not run')")
    assert (tmp_path / "model.py").exists()
