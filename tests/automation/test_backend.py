from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("aiida")
from aiida import orm
from aiida.tools.pytest_fixtures.configuration import (  # noqa: F401, E402
    aiida_config_factory,
    aiida_profile_factory,
)

from chemistry_agent.automation import backend  # noqa: E402
from chemistry_agent.automation.models import PreviewRequest, StructureInput  # noqa: E402
from chemistry_agent.errors import AgentError  # noqa: E402

POSCAR = "Cu test\n1.0\n3.6 0 0\n0 3.6 0\n0 0 3.6\nCu\n1\nDirect\n0 0 0\n"


@pytest.fixture
def profile(tmp_path, aiida_config_factory, aiida_profile_factory):  # noqa: F811
    with aiida_config_factory(tmp_path) as config, aiida_profile_factory(config) as profile:
        yield profile


def make_payload(scheduler: str = "slurm") -> dict:
    computer = orm.Computer(
        label=str(uuid4()),
        hostname="localhost",
        description=backend.MARKER,
        transport_type="core.local",
        scheduler_type=f"core.{scheduler}",
        workdir="/tmp/chem-agent-tests",
    ).store()
    computer.configure()
    orm.InstalledCode(
        label=f"structure-probe-{computer.uuid[:8]}",
        computer=computer,
        filepath_executable="/bin/sh",
        default_calc_job_plugin="chemistry.structure_probe",
    ).store()
    return {
        "server_id": computer.uuid,
        "structure": {"content": POSCAR},
        "resources": {"queue": "test-queue"},
    }


@pytest.mark.parametrize(
    "scheduler,directive",
    [
        ("slurm", "#SBATCH --partition=test-queue"),
        ("pbspro", "#PBS -q test-queue"),
        ("torque", "#PBS -q test-queue"),
    ],
)
def test_real_scheduler_dry_run(profile, scheduler, directive):
    result = backend.preview(make_payload(scheduler))
    assert directive in result["files"]["_aiidasubmit.sh"]
    assert "Cu" in result["files"]["POSCAR"]
    assert result["structure"]["atoms"] == 1
    assert orm.QueryBuilder().append(orm.CalcJobNode).count() == 0


def test_repeated_submit_and_config_change(profile, monkeypatch):
    result = backend.preview(make_payload())
    monkeypatch.setattr(
        backend, "get_daemon_client", lambda: SimpleNamespace(is_daemon_running=True)
    )
    calls = []

    def fake_submit(builder):
        calls.append(builder)
        node = orm.CalcJobNode(label=builder.metadata.label).store()
        return node

    monkeypatch.setattr(backend, "submit", fake_submit)
    first = backend.submit_preview({"preview_id": result["preview_id"]})
    repeated = backend.submit_preview({"preview_id": result["preview_id"]})
    assert first["job"]["id"] == repeated["job"]["id"]
    assert repeated["reused"]
    assert len(calls) == 1
    other = backend.preview(make_payload())
    code = orm.load_code(uuid=other["code_uuid"])
    code.computer.set_prepend_text("echo changed")
    with pytest.raises(AgentError, match="Code 已变化"):
        backend.submit_preview({"preview_id": other["preview_id"]})
    assert len(calls) == 1


def test_unknown_submit_outcome_is_not_retried(profile, monkeypatch):
    result = backend.preview(make_payload())
    monkeypatch.setattr(
        backend, "get_daemon_client", lambda: SimpleNamespace(is_daemon_running=True)
    )

    def interrupted_submit(builder):
        raise ConnectionError("lost response")

    monkeypatch.setattr(backend, "submit", interrupted_submit)
    with pytest.raises(ConnectionError):
        backend.submit_preview({"preview_id": result["preview_id"]})
    with pytest.raises(AgentError) as error:
        backend.submit_preview({"preview_id": result["preview_id"]})
    assert error.value.code == "submission_uncertain"


def test_rejects_constraints_and_missing_periodic_cell():
    fixed = POSCAR.replace("Direct", "Selective dynamics\nDirect").replace(
        "0 0 0\n", "0 0 0 F F F\n"
    )
    with pytest.raises(AgentError, match="约束"):
        backend.read_structure(StructureInput(content=fixed))
    with pytest.raises(AgentError):
        backend.read_structure(StructureInput(format="extxyz", content="1\n\nCu 0 0 0\n"))


def test_probe_does_not_accept_gpu_or_large_resources(profile):
    payload = make_payload()
    payload["resources"]["gpus_per_node"] = 1
    with pytest.raises(AgentError) as error:
        backend.build_job(PreviewRequest.model_validate(payload), "probe")
    assert error.value.code == "probe_resource_limit"
