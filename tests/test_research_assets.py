"""Exercise the live platform's asset contracts with a pre-Agent VASP directory."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.knowledge.asset_vasp import vasp_facts

STRUCTURE = """Cu slab
1.0
3 0 0
0 3 0
0 0 16
Cu
2
Direct
0 0 0
0.5 0.5 0.5
"""
OUTCAR = b""" vasp.6.4.3
  TITEL  = PAW_PBE Cu 22Jun2005
  GGA = PE
  IBRION = -1    NSW = 0
  Iteration 1(2)
  free energy    TOTEN  =  -13.02 eV
  aborting loop because EDIFF is reached
  FREE ENERGIE OF THE ION-ELECTRON SYSTEM (eV)
  ---------------------------------------------------
  free  energy   TOTEN  =  -12.75 eV
  General timing and accounting informations for this job:
"""


def call(client: TestClient, name: str, args: dict, project: str | None = None) -> dict:
    response = client.post(
        "/api/knowledge/mcp",
        params={"project_id": project} if project else None,
        json={"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def value(result: dict) -> dict:
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_legacy_asset_available_across_service_restarts_and_portable(settings_factory, tmp_path):
    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("an asset operation called a model")

    def client() -> TestClient:
        return TestClient(
            create_app(settings_factory(), transport=httpx.MockTransport(unexpected)),
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 20000),
        )

    source = tmp_path / "manual-calculation"
    source.mkdir()
    (source / "CONTCAR").write_text(STRUCTURE)
    (source / "OUTCAR").write_bytes(OUTCAR)
    (source / "analysis.py").write_text("print('original')\n")
    arguments = {
        "directory": str(source),
        "files": ["analysis.py"],
        "title": "Cu 表面优化证据",
    }
    with client() as api:
        project = api.post("/api/projects", json={"name": "原工作组"}).json()["id"]
        initial = value(call(api, "asset_import", arguments, project))
        assert value(call(api, "asset_import", arguments, project))["unchanged"]
        (source / "analysis.py").write_text("print('edited')\n")
        edited = value(call(api, "asset_import", arguments, project))
        assert edited["id"] == initial["id"]
        assert edited["version"] == 2
        (source / "analysis.py").unlink()
        (source / "CONTCAR").unlink()
        (source / "OUTCAR").unlink()

    with client() as api:
        assert not value(call(api, "asset_search", {}, None))["items"]
        hits = value(
            call(
                api,
                "asset_search",
                {"query": "优化", "formula": "Cu2", "functional": "PBE"},
                project,
            )
        )["items"]
        assert [(r["id"], r["version"]) for r in hits] == [(initial["id"], 2)]
        assert not value(call(api, "asset_search", {"functional": "PBEsol"}, project))["items"]
        ref = {"asset_id": initial["id"], "version": 1}
        item = value(call(api, "asset_inspect", ref, project))
        facts = item["manifest"]["facts"]
        assert facts["energy"] == {
            "value": -12.75,
            "metric": "TOTEN",
            "unit": "eV",
            "source": "OUTCAR",
        }
        assert facts["electronic_converged"] is True
        assert facts["ionic_converged"] is None
        assert item["scientifically_validated"] is False
        checked_out = value(
            call(api, "asset_checkout", ref | {"directory": str(tmp_path)}, project)
        )
        bundle = Path(checked_out["directory"])
        assert (bundle / "analysis.py").read_text() == "print('original')\n"
        imported = value(call(api, "asset_import", {"directory": str(bundle), "kind": "bundle"}))
        assert imported["id"] != initial["id"]
        assert imported["revision_sha256"] == initial["revision_sha256"]
        (bundle / "analysis.py").write_text("print('tampered')\n")
        assert call(api, "asset_import", {"directory": str(bundle), "kind": "bundle"})["isError"]
        assert call(api, "asset_import", {"directory": str(tmp_path / "archive"), "kind": "vasp"})[
            "isError"
        ]


def test_scf_trace_does_not_look_like_final_calculation_energy():
    incomplete = OUTCAR + b" Iteration 2(1)\n free energy TOTEN = -12.99 eV\n"
    for output in (b" free energy TOTEN = -55 eV\n", incomplete):
        facts = vasp_facts({}, output, output, len(output))
        assert facts["energy"] is None
        assert facts["electronic_converged"] is None
        assert facts["output_complete"] is False
