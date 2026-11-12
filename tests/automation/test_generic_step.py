from types import SimpleNamespace

import pytest

from chemistry_agent.automation.generic_step import complete_frames, inspect_generic_step
from chemistry_agent.automation.slurm_remote import vasp_snapshot
from chemistry_agent.errors import AgentError

POSCAR = "Cu\n1\n3 0 0\n0 3 0\n0 0 3\nCu\n1\nDirect\n0 0 0\n"
XDATCAR = "Cu\n1\n3 0 0\n0 3 0\n0 0 3\nCu\n1\nDirect configuration=1\n0 0 0\n"


def files(root, energy):
    root.mkdir()
    for name, text in {
        "POSCAR": POSCAR,
        "CONTCAR": POSCAR,
        "XDATCAR": XDATCAR,
        "INCAR": "ENCUT=520\nNSW=150\n",
        "KPOINTS": "Gamma\n0\nG\n4 4 1\n",
        "OSZICAR": f" 1 F= {energy} E0= {energy}\n 2 F= -9 E0= -9",
        "submit.sh": "#!/bin/bash\nsbatch --help\n",
    }.items():
        (root / name).write_text(text)


async def test_task_job_selection_and_retrieved_vs_live(tmp_path):
    stored, live = tmp_path / "stored", tmp_path / "live"
    files(stored, -2)
    files(live, -3)
    records = {
        "old": {
            "id": "old",
            "terminal": True,
            "retrieved_directory": str(stored),
            "scheduler_job_id": "100",
        },
        "new": {"id": "new", "terminal": False, "scheduler_job_id": "101"},
    }
    calls = []

    async def remote(record, action, **kwargs):
        calls.append(record["id"])
        assert action == "vasp_snapshot"
        return vasp_snapshot(live, kwargs["file"])

    slurm = SimpleNamespace(load=lambda session, key: records[key], remote=remote)
    chat = SimpleNamespace(plans=SimpleNamespace(slurm=slurm))
    node = {"id": "bulk", "kind": "task"}
    plan = {
        "revision": 1,
        "status": "running",
        "nodes": [node],
        "runtime": {
            "bulk": {"status": "running", "jobs": ["old", "new"], "accepted_jobs": ["old"]}
        },
    }
    info = await inspect_generic_step(chat, "session", plan, node, "info", None)
    assert info["selected_job_id"] == "old" and info["points"] == [[1, -2.0]]
    assert info["files"]["POSCAR"] == "已取回" and not calls
    assert "520" in info["parameters"]["INCAR"] and info["script"].startswith("#!")
    live_info = await inspect_generic_step(chat, "session", plan, node, "info", "new")
    assert live_info["points"] == [[1, -3.0]] and calls == ["new"]
    assert live_info["files"]["CONTCAR"] == "作业目录快照"
    result = await inspect_generic_step(chat, "session", plan, node, "POSCAR", "old")
    assert result["content"] == POSCAR
    with pytest.raises(AgentError, match="不属于"):
        await inspect_generic_step(chat, "session", plan, node, "info", "other")
    # Retrieving a missing trajectory must not replace immutable local results.
    (stored / "XDATCAR").unlink()
    mixed = await inspect_generic_step(chat, "session", plan, node, "info", "old")
    assert mixed["points"] == [[1, -2.0]]
    assert mixed["files"] == {"POSCAR": "已取回", "CONTCAR": "已取回", "XDATCAR": "作业目录快照"}


def test_partial_trajectory_and_snapshot_bounds(tmp_path):
    assert complete_frames(XDATCAR + "Direct configuration=2\n0 0") == XDATCAR
    (tmp_path / "POTCAR").write_text("not for preview")
    (tmp_path / "POSCAR").symlink_to(tmp_path.parent / "outside")
    with pytest.raises(ValueError, match="越过"):
        vasp_snapshot(tmp_path)
