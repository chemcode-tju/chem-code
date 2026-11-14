"""Result registration and viewing must never submit or modify a calculation."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from test_slurm_jobs import cluster as cluster_fixture

from chemistry_agent.api import create_app
from chemistry_agent.automation.result_reader import result_snapshot
from chemistry_agent.automation.slurm_jobs import REMOTE_HELPER
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.records import RecordRepository
from chemistry_agent.platform.results import (
    ResultInspect,
    ResultOperations,
    ResultRegister,
    ResultSource,
)

cluster = cluster_fixture

POSCAR = "Cu\n1\n3 0 0\n0 3 0\n0 0 3\nCu\n1\nDirect\n0 0 0\n"


def path_step(root: Path, step: int, complete: bool = True) -> None:
    folder = root / f"paths/step_{step:03d}"
    folder.mkdir(parents=True)
    for i in range(3):
        (folder / f"image_{i:02d}.vasp").write_text(POSCAR)
    (folder / "path.json").write_text(
        json.dumps(
            {
                "step": step,
                "energy_TOTEN_eV": [-10, -8 - step / 10, -9],
                "projected_fmax_eV_A": 0.2 / (step + 1),
                "all_scf_valid": complete,
            }
        )
    )


def source() -> dict:
    return ResultSource(
        adapter="json_paths", pattern="paths/step_*/path.json", force_target=0.02
    ).model_dump(mode="json")


def test_snapshot_complete_paths_only_and_no_compute(tmp_path):
    path_step(tmp_path, 0)
    path_step(tmp_path, 1)
    path_step(tmp_path, 2, complete=False)
    (tmp_path / "INCAR").write_text("NSW=0")
    (tmp_path / "force_calls").mkdir()
    before = {str(p): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    result = result_snapshot(tmp_path, source())
    assert [r["step"] for r in result["snapshots"]] == [0, 1]
    assert result["snapshots"][1]["images"][1]["relative_energy"] == pytest.approx(1.9)
    assert result["snapshots"][1]["force"] == 0.1
    assert len(result["skipped"]) == 1
    assert result_snapshot(tmp_path, source(), "paths/step_000/image_00.vasp")["content"] == POSCAR
    assert before == {str(p): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    with pytest.raises(ValueError, match="未登记"):
        result_snapshot(tmp_path, source(), "INCAR")
    # A half-written new JSON must not hide older valid snapshots.
    (tmp_path / "paths/step_002/path.json").write_text('{"step":')
    assert len(result_snapshot(tmp_path, source())["snapshots"]) == 2


def test_portable_manifest_arbitrary_layout_and_scope(tmp_path):
    folder = tmp_path / "anything/geometry"
    folder.mkdir(parents=True)
    (folder / "done.vasp").write_text(POSCAR)
    manifest = {
        "structures": [{"path": "anything/geometry/done.vasp", "label": "末态"}],
        "series": [
            {
                "label": "温度",
                "x_label": "时间",
                "y_label": "T",
                "unit": "K",
                "points": [[0, 300], [1, 302]],
            }
        ],
    }
    (tmp_path / "display.json").write_text(json.dumps(manifest))
    data = result_snapshot(tmp_path, {"adapter": "manifest", "path": "display.json"})
    assert data["series"][0]["points"][1] == [1, 302]
    assert data["structures"][0]["label"] == "末态"
    outside = tmp_path.parent / "private.vasp"
    outside.write_text(POSCAR)
    (tmp_path / "link.vasp").symlink_to(outside)
    for path in ["../private.vasp", "link.vasp", "archive/POSCAR", str(outside)]:
        with pytest.raises(ValueError):
            result_snapshot(tmp_path, {"adapter": "inline", "structures": [{"path": path}]})
    with pytest.raises(ValidationError):
        ResultSource(adapter="json_paths", pattern="**/path.json")


def test_remote_helper_is_self_contained(tmp_path):
    path_step(tmp_path, 0)
    response = subprocess.run(
        [sys.executable, "-I", "-c", REMOTE_HELPER],
        input=json.dumps({"root": str(tmp_path), "action": "result_snapshot", "source": source()}),
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(response.stdout)
    assert data["snapshots"][0]["step"] == 0


async def test_registry_idempotency_job_binding_cache_and_disconnect(tmp_path):
    records = RecordRepository(tmp_path / "records")
    session, plan_id, job_id = map(str, [uuid4(), uuid4(), uuid4()])
    plan = {"id": plan_id, "runtime": {"neb": {"jobs": [job_id]}}}
    records.save(session, "plans", plan_id, plan)
    records.save(session, "slurm_jobs", job_id, {"id": job_id})
    live = tmp_path / "job"
    path_step(live, 0)
    calls = []
    offline = False

    async def remote(record, action, **kwargs):
        calls.append(action)
        if offline:
            raise AgentError("job_ssh_failed", "offline", 503)
        assert action == "result_snapshot"
        return result_snapshot(live, kwargs["source"], kwargs.get("file"), kwargs.get("path_step"))

    chat = SimpleNamespace(load=records.load, save=records.save, list_records=records.list_records)
    plans = SimpleNamespace(
        chat=chat,
        slurm=SimpleNamespace(load=lambda s, j: records.load(s, "slurm_jobs", j), remote=remote),
    )
    service = ResultOperations(plans)
    context = SimpleNamespace(session_id=session)
    args = ResultRegister(
        plan_id=plan_id, node_id="neb", job_id=job_id, key="path", title="CI-NEB", source=source()
    )
    first = await service.register(args, context)
    second = await service.register(args, context)
    assert first == second
    assert (
        first["id"] == second["id"] and len(records.list_records(session, "result_registry")) == 1
    )
    query = ResultInspect(plan_id=plan_id, node_id="neb", job_id=job_id, key="path")
    assert len((await service.inspect(query, context))["snapshots"]) == 1
    await service.inspect(query, context)
    assert calls == ["result_snapshot"]
    file_query = query.model_copy(update={"file": "paths/step_000/image_00.vasp"})
    assert (await service.inspect(file_query, context))["content"] == POSCAR
    offline = True
    # Persistence works across service restarts, including previously viewed structures.
    restarted = ResultOperations(plans)
    await restarted.register(args, context)
    assert (await restarted.inspect(query, context))["stale"] is True
    assert (await restarted.inspect(file_query, context))["stale"] is True
    with pytest.raises(AgentError, match="不属于"):
        await service.register(args.model_copy(update={"job_id": uuid4()}), context)
    assert records.load(session, "plans", plan_id) == plan


async def test_http_registered_results_are_routed_before_generic_step(settings_factory, tmp_path):
    app = create_app(settings_factory(automation_enabled=True))
    async with app.router.lifespan_context(app):
        session = (await app.state.store.create_session("results"))["id"]
        plan_id, job_id = str(uuid4()), str(uuid4())
        chat = app.state.calculation_chat
        chat.save(
            session, "plans", plan_id, {"id": plan_id, "runtime": {"neb": {"jobs": [job_id]}}}
        )
        chat.save(session, "slurm_jobs", job_id, {"id": job_id})
        path_step(tmp_path, 0)

        async def remote(record, action, **kwargs):
            assert action == "result_snapshot"
            return result_snapshot(
                tmp_path, kwargs["source"], kwargs.get("file"), kwargs.get("path_step")
            )

        chat.plans.slurm.remote = remote
        body = {
            "plan_id": plan_id,
            "node_id": "neb",
            "job_id": job_id,
            "key": "path",
            "title": "CI-NEB",
            "source": source(),
        }
        base = f"/api/automation/chat/{session}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            registered = await client.post(base + "/results", json=body)
            assert registered.status_code == 200, registered.text
            route = base + f"/workflows/{plan_id}/steps/neb/results"
            listing = await client.get(route, params={"job_id": job_id})
            assert listing.json()["datasets"][0]["key"] == "path"
            data = await client.get(route, params={"job_id": job_id, "key": "path"})
            assert data.json()["snapshots"][0]["step"] == 0
            sequence = await client.get(
                route, params={"job_id": job_id, "key": "path", "path_step": 0}
            )
            assert sequence.status_code == 200
            assert len(sequence.json()["files"]) == 3
            file = await client.get(
                route,
                params={"job_id": job_id, "key": "path", "file": "paths/step_000/image_01.vasp"},
            )
            assert file.status_code == 200 and file.text == POSCAR
            other = await client.get(route, params={"job_id": str(uuid4()), "key": "path"})
            assert other.status_code == 404


def test_default_manifest_can_appear_later_without_reregistration(tmp_path):
    assert result_snapshot(tmp_path, {"adapter": "auto"})["available"] is False
    (tmp_path / "display.json").write_text(
        json.dumps(
            {
                "structures": [],
                "series": [
                    {
                        "label": "E",
                        "x_label": "step",
                        "y_label": "E",
                        "unit": "eV",
                        "points": [[0, -3]],
                    }
                ],
            }
        )
    )
    assert result_snapshot(tmp_path, {"adapter": "auto"})["series"][0]["points"] == [[0, -3]]


async def test_adopt_one_word_hint_registers_without_extra_remote_call(cluster, monkeypatch):
    from test_job_adoption import scheduler

    from chemistry_agent.automation.job_adoption import adopt_job
    from chemistry_agent.automation.job_models import JobAdopt

    plans, context, tmp = cluster
    remote = tmp / "external"
    path_step(remote, 0)
    calls = scheduler(monkeypatch, remote)
    binding = await adopt_job(
        plans, JobAdopt(host="test", scheduler_job_id="42", results="neb"), context
    )
    assert binding["results_registered"]
    assert [c[0] for c in calls] == ["scontrol", "scontrol"]
    registrations = plans.chat.list_records(context.session_id, "result_registry")
    assert len(registrations) == 1
    assert registrations[0]["source"]["adapter"] == "json_paths"
    assert registrations[0]["source"]["pattern"] == "paths/step_*/path.json"
    # Omitted display hint on adoption replay must preserve the prior explicit mapping.
    await adopt_job(plans, JobAdopt(host="test", scheduler_job_id="42"), context)
    assert plans.chat.list_records(context.session_id, "result_registry") == registrations
    inspected = await plans.platform.results.inspect(
        ResultInspect(
            plan_id=binding["plan_id"],
            node_id=binding["node_id"],
            job_id=binding["job_id"],
            key="job-results",
        ),
        context,
    )
    assert len(inspected["snapshots"]) == 1
    assert not any(c[0] == "sbatch" for c in calls)


async def test_platform_submitted_job_gets_default_binding_without_agent(cluster):
    from test_slurm_jobs import draft, job, tick

    plans, context, tmp = cluster
    identifier = await draft(plans, context, [job(tmp)])
    await tick(plans, context, identifier)
    rows = plans.chat.list_records(context.session_id, "result_registry")
    assert len(rows) == 1 and rows[0]["source"]["adapter"] == "auto"
    plan = plans.chat.load(context.session_id, "plans", identifier)
    assert rows[0]["job_id"] == plan["runtime"]["compute"]["generic_job_id"]
    await tick(plans, context, identifier)
    assert len(plans.chat.list_records(context.session_id, "result_registry")) == 1
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 1


def test_auto_recognizes_existing_neb_convention_without_model_hint(tmp_path):
    path_step(tmp_path, 0)
    auto = ResultSource(adapter="auto").model_dump(mode="json")
    assert result_snapshot(tmp_path, auto)["kind"] == "neb"
    assert result_snapshot(tmp_path, auto, "paths/step_000/image_01.vasp")["content"] == POSCAR


def test_image_force_mapping_excludes_fixed_endpoints_and_raw_vasp_force(tmp_path):
    path_step(tmp_path, 0)
    path = tmp_path / "paths/step_000/path.json"
    data = json.loads(path.read_text())
    data.update(image_projected_fmax_eV_A=[0.2], raw_forces_eV_A=[100, 100, 100])
    path.write_text(json.dumps(data))
    snapshot = result_snapshot(tmp_path, source())["snapshots"][0]
    assert [image["force"] for image in snapshot["images"]] == [None, 0.2, None]
    data["image_projected_fmax_eV_A"] = [0.2, 0.3]
    path.write_text(json.dumps(data))
    invalid = result_snapshot(tmp_path, source())
    assert not invalid["snapshots"] and len(invalid["skipped"]) == 1


def test_endpoint_outcar_reference_is_separate_and_partial_reads_preserve_path(tmp_path):
    path_step(tmp_path, 0)
    output = tmp_path / "initial.OUTCAR"
    complete = """ Iteration 1( 1)
 POSITION TOTAL-FORCE (eV/Angst)
 ----------------------------------
 0 0 0 0.03 0.04 0
 ----------------------------------
 free energy TOTEN = -10.5 eV
"""
    output.write_text(
        complete
        + """ Iteration 2( 1)
 POSITION TOTAL-FORCE (eV/Angst)
 ----------------------------------
 0 0 0 9 9 9
"""
    )
    settings = source() | {
        "endpoint_outcars": {"initial": "initial.OUTCAR", "final": "missing.OUTCAR"}
    }
    result = result_snapshot(tmp_path, settings)
    images = result["snapshots"][0]["images"]
    assert images[0]["energy"] == -10
    assert images[0]["force"] is None
    assert images[0]["endpoint_reference"] == {
        "energy": -10.5,
        "atomic_fmax": 0.05,
        "atoms": 1,
        "source": "initial.OUTCAR",
    }
    assert images[-1]["endpoint_reference"] is None
    assert result["skipped"][0]["path"] == "missing.OUTCAR"
    assert len(result["snapshots"]) == 1
    with pytest.raises(ValidationError):
        ResultSource(endpoint_outcars={"initial": "../OUTCAR"})
    # Standalone SSH helper includes the parser, with no external packages.
    response = subprocess.run(
        [sys.executable, "-I", "-c", REMOTE_HELPER],
        input=json.dumps({"root": str(tmp_path), "action": "result_snapshot", "source": settings}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert (
        json.loads(response.stdout)["snapshots"][0]["images"][0]["endpoint_reference"]["energy"]
        == -10.5
    )


def test_neb_sequence_reads_one_complete_step_in_image_order(tmp_path):
    path_step(tmp_path, 0)
    path_step(tmp_path, 1)
    path_step(tmp_path, 2, complete=False)
    result = result_snapshot(tmp_path, source(), path_step=1)
    assert result["kind"] == "neb_sequence" and result["step"] == 1
    assert [item["index"] for item in result["files"]] == [0, 1, 2]
    assert all(item["content"] == POSCAR for item in result["files"])
    assert (
        result_snapshot(tmp_path, ResultSource(adapter="auto").model_dump(), path_step=0)["step"]
        == 0
    )
    with pytest.raises(ValueError, match="完整路径"):
        result_snapshot(tmp_path, source(), path_step=2)
    with pytest.raises(ValueError, match="单个结构"):
        result_snapshot(tmp_path, source(), "paths/step_000/image_00.vasp", path_step=0)
    response = subprocess.run(
        [sys.executable, "-I", "-c", REMOTE_HELPER],
        input=json.dumps(
            {"root": str(tmp_path), "action": "result_snapshot", "source": source(), "path_step": 1}
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(response.stdout)["files"] == result["files"]
