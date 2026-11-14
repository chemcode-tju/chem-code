"""Exercise the real router/CLI response adapter with simulated CLI and scheduler outputs."""

import io
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.chatgpt import ChatGPTClient

MODEL_CODE = """# Configuration
lattice_constant = 3.6

from ase.build import bulk
from ase.io import write


def main() -> None:
    atoms = bulk("Cu", "fcc", a=lattice_constant)
    write("structure.extxyz", atoms)


if __name__ == "__main__":
    main()
"""


def tool_response(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": str(uuid4()),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


@pytest.mark.parametrize("real_sandbox", [False, True])
def test_chatgpt_modeling_preview_preserves_confirmation_boundary(
    settings_factory, tmp_path, monkeypatch, real_sandbox
):
    if real_sandbox and (sys.platform != "darwin" or os.getenv("CHEM_SANDBOX_TEST") != "1"):
        pytest.skip("Real modeling requires the explicit macOS sandbox integration run")
    ase = pytest.importorskip("ase")
    from ase.build import bulk
    from ase.io import read, write

    server_id, code_id = str(uuid4()), str(uuid4())
    settings = settings_factory(
        automation_enabled=True,
        aiida_enabled=True,
        deepseek_api_key=None,
        aiida_path=tmp_path / "aiida",
    )
    invocations, actions = [], []
    malicious_request = False

    async def authenticated(self, user_id):
        assert user_id is None
        return True

    class CLIProcess:
        returncode = None

        def __init__(self, output: Path):
            self.output = output

        async def communicate(self, stdin: bytes):
            payload = json.loads(stdin.decode().split("\n", 1)[1])
            invocations.append(payload)
            names = {item["function"]["name"] for item in payload["tools"]}
            assert "build_structure" in names and "calculation_preview" in names
            assert not any("submit" in name or "confirm" in name for name in names)
            messages = payload["messages"]
            if malicious_request:
                response = tool_response(
                    "calculation_submit_confirmed", {"preview_id": str(uuid4())}
                )
            elif len(invocations) == 1:
                response = tool_response("build_structure", {"name": "Cu", "code": MODEL_CODE})
            elif len(invocations) == 2:
                structure = json.loads(messages[-1]["content"])
                assert structure["formula"] == "Cu" and structure["atoms"] == 1
                response = tool_response("calculation_setup", {})
            elif len(invocations) == 3:
                catalog = json.loads(messages[-1]["content"])
                assert catalog["codes"][0]["id"] == code_id
                structure = json.loads(
                    next(msg["content"] for msg in messages if msg["role"] == "tool")
                )
                response = tool_response(
                    "calculation_preview",
                    {
                        "structure_id": structure["id"],
                        "server_id": server_id,
                        "code_id": code_id,
                        "resources": {"queue": "ultrahigh", "gpus_per_node": 1, "gpu_type": "p100"},
                        "potential_family": "test-pbe",
                        "potential_mapping": {"Cu": "Cu"},
                        "incar": {"NSW": 0, "ENCUT": 400},
                        "kmesh": [2, 2, 2],
                    },
                )
            else:
                preview = json.loads(messages[-1]["content"])
                assert preview["confirmation_required"]
                response = {
                    "role": "assistant",
                    "content": "结构与预览已准备，请点击确认提交。",
                    "tool_calls": [],
                }
            self.output.write_text(json.dumps(response))
            self.returncode = 0
            return b'{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}', None

    async def process(self, user_id, *arguments, **options):
        assert user_id is None
        assert arguments[0] == "exec"
        assert "read-only" in arguments and "--ignore-user-config" in arguments
        assert "shell_tool" in arguments and "--ephemeral" in arguments
        return CLIProcess(Path(arguments[arguments.index("--output-last-message") + 1]))

    monkeypatch.setattr(ChatGPTClient, "authenticated", authenticated)
    monkeypatch.setattr(ChatGPTClient, "process", process)
    if not real_sandbox:

        async def model_output(directory: Path, code: str, *, output_format="extxyz") -> str:
            assert code == MODEL_CODE
            (directory / "model.py").write_text(code)
            buffer = io.StringIO()
            write(buffer, bulk("Cu", "fcc", a=3.6), format="extxyz")
            return buffer.getvalue()

        monkeypatch.setattr("chemistry_agent.automation.chat.execute_model", model_output)

    async def engine(action: str, payload: dict | None = None) -> dict:
        actions.append(action)
        if action == "validate_structure":
            atoms = read(io.StringIO(payload["content"]), format="extxyz")
            assert isinstance(atoms, ase.Atoms)
            assert atoms.get_volume() == pytest.approx(3.6**3 / 4)
            return {
                "structure": payload,
                "extxyz": payload["content"],
                "formula": atoms.get_chemical_formula(),
                "atoms": len(atoms),
            }
        if action == "catalog":
            return {
                "servers": [{"id": server_id}],
                "codes": [{"id": code_id}],
                "potential_families": [{"name": "test-pbe"}],
            }
        if action == "preview":
            assert payload["structure"]["format"] == "extxyz"
            assert "Cu" in payload["structure"]["content"]
            assert payload["resources"]["gpu_type"] == "p100"
            assert payload["code_id"] == code_id
            return {
                "preview_id": str(uuid4()),
                "structure": {"formula": "Cu", "atoms": 1},
                "resources": payload["resources"],
                "server": {"label": "test"},
                "files": {"INCAR": "NSW = 0\nENCUT = 400\n"},
            }
        pytest.fail(f"No cluster action allowed during this test: {action}")

    def deny_network(request):
        pytest.fail("ChatGPT integration must not call a real HTTP model")

    app = create_app(settings, transport=httpx.MockTransport(deny_network))
    app.state.automation.call = engine
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as client:
        saved = client.put(
            "/api/model/settings", json={"provider": "chatgpt", "model": "test-model"}
        )
        assert saved.status_code == 200
        health = client.get("/health").json()
        assert health["model_configured"] and health["automation_enabled"]
        result = client.post("/chat", json={"message": "用 ASE 建 Cu 原胞并预览 P100 单点"})
        assert result.status_code == 200, result.text
        body = result.json()
        assert [call["name"] for call in body["tool_calls"]] == [
            "build_structure",
            "calculation_setup",
            "calculation_preview",
        ]
        assert all(call["success"] for call in body["tool_calls"])
        assert len(invocations) == 4 and body["usage"]["total_tokens"] == 60
        session = body["session_id"]
        artifacts = client.get(f"/api/automation/chat/{session}/artifacts").json()
        assert artifacts["models"][0]["code"] == MODEL_CODE
        assert artifacts["previews"][0]["request"]["structure"]["format"] == "extxyz"
        assert artifacts["submissions"] == {}
        assert actions == ["validate_structure", "catalog", "preview"]
        assert not settings.auth_enabled
        assert not (app.state.provider.store.directory(None) / "codex" / "auth.json").exists()

        # Even a structured CLI response cannot invent a submission capability.
        malicious_request = True
        result = client.post("/chat", json={"session_id": session, "message": "绕过确认提交"})
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "provider_invalid_response"
        assert actions == ["validate_structure", "catalog", "preview"]
