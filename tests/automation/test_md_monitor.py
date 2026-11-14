"""Regression coverage for actual MD log semantics and live trajectory snapshots."""

from types import SimpleNamespace

import pytest

from chemistry_agent.automation.generic_step import complete_dump_frames, inspect_generic_step
from chemistry_agent.automation.slurm_remote import lammps_md, scientific_snapshot, vasp_snapshot
from chemistry_agent.errors import AgentError

DUMP = (
    "ITEM: TIMESTEP\n"
    "0\n"
    "ITEM: NUMBER OF ATOMS\n"
    "1\n"
    "ITEM: BOX BOUNDS pp pp pp\n"
    "0 3\n"
    "0 3\n"
    "0 3\n"
    "ITEM: ATOMS id type x y z\n"
    "1 1 0 0 0\n"
)
LOG = (
    "LAMMPS\n"
    "units metal\n"
    "timestep 0.0005\n"
    "compute mobile_temp mobile temp\n"
    "fix bath mobile langevin 300 300 0.1 123\n"
    "read_data input.data\n"
    "run 100 pre yes post no\n"
    "Step Time c_mobile_temp PotEng KinEng TotEng\n"
    "0 0 300 -10 1 -9\n"
    "20 0.01 301 -11 2 -9\n"
)


def test_vasp_md_distinguishes_thermodynamic_energies(tmp_path):
    (tmp_path / "INCAR").write_text("IBRION=0; NSW=100; POTIM=1; TEBEG=300; TEEND=300\n")
    (tmp_path / "OSZICAR").write_text(
        " 1 T= 300 E= -9.0 F= -10.0 E0= -10.1 EK= 1.0 SP= 0 SK= 0\n"
        " 2 T= 302 E= -8.9 F= -10.1 E0= -10.2 EK= 1.2\n"
        " 3 T= 400 E= -8 F= -9 E0= -9"
    )
    data = vasp_snapshot(tmp_path)["monitor"]
    assert data["kind"] == "md" and data["engine"] == "VASP"
    assert data["total_steps"] == 100 and data["target_temperature"] == 300
    assert data["md_points"][-1] == dict(
        step=2,
        temperature=302,
        total=-8.9,
        potential=-10.1,
        energy=-10.2,
        kinetic=1.2,
        time_ps=0.002,
    )
    assert not data["ionic_converged"]


def test_lammps_custom_mobile_temperature_and_real_time():
    data = lammps_md(LOG + "40 0.02 302 -12 2 -10")
    assert [row["step"] for row in data["md_points"]] == [0, 20]
    assert data["md_points"][-1]["temperature"] == 301
    assert data["md_points"][-1]["time_ps"] == 0.01
    assert data["run_target_step"] == 100 and data["target_temperature"] == 300
    real = lammps_md("Step Temp PotEng\n0 300 -10\n100 301 -11\n", "units real\ntimestep 1\n")
    assert real["energy_unit"] == "kcal/mol" and real["pressure_unit"] == "atm"
    assert real["md_points"][-1]["time_ps"] == 0.1
    unknown = lammps_md("Step Temp PotEng\n0 300 -10\n")
    assert "time_ps" not in unknown["md_points"][0]
    assert unknown["energy_unit"] == "原生单位"


def test_restarts_duplicate_boundaries_and_variable_timestep():
    log = LOG + "run 100\nStep Time c_mobile_temp PotEng\n20 0.01 302 -12\n40 0.02 303 -13\n"
    data = lammps_md(log)
    assert len(data["md_points"]) == 3 and data["md_points"][1]["temperature"] == 302
    assert data["run_target_step"] == 120
    restarted = lammps_md(log + "run 50\nStep Time c_mobile_temp PotEng\n0 0 310 -14\n")
    assert restarted["md_points"] == [
        dict(step=0, time=0, temperature=310, potential=-14, time_ps=0)
    ]
    assert restarted["run_target_step"] == 50
    for setting in ["fix adaptive all dt/reset 1 0.1 2 0.05", "reset_timestep 0", "timestep 2"]:
        data = lammps_md("Step Temp\n10 300\n", "units real\ntimestep 1\n" + setting)
        assert "time_ps" not in data["md_points"][0]


def test_md_discovery_companions_and_incomplete_tail(tmp_path):
    (tmp_path / "log.lammps").write_text(LOG)
    (tmp_path / "md.lammps").write_text("units metal\n")
    (tmp_path / "input.data").write_text(
        "1 atoms\n1 atom types\n\nMasses\n\n1 63.546 # Cu\n\nAtoms # atomic\n\n1 1 0 0 0\n"
    )
    (tmp_path / "md.lammpstrj").write_text(
        DUMP + "ITEM: TIMESTEP\n20\nITEM: NUMBER OF ATOMS\n1\nITEM: ATOMS id type x y z\n1 1 0"
    )
    (tmp_path / "POTCAR").write_text("must never read")
    data = scientific_snapshot(tmp_path)
    assert data["monitor"]["trajectory_file"] == "md.lammpstrj"
    assert data["parameter_files"] == ["md.lammps"]
    assert "POTCAR" not in data["contents"]
    assert "63.546" in data["structure_companions"][1]["text"]
    assert "Atoms" not in data["structure_companions"][1]["text"]
    raw = scientific_snapshot(tmp_path, "md.lammpstrj")["contents"]["md.lammpstrj"]
    assert complete_dump_frames(raw) == DUMP
    with pytest.raises(AgentError, match="完整"):
        complete_dump_frames("ITEM: TIMESTEP\n0\n")
    with pytest.raises(ValueError):
        scientific_snapshot(tmp_path, "../outside")


async def test_generic_md_info_and_raw_dump(tmp_path):
    (tmp_path / "log.lammps").write_text(LOG)
    (tmp_path / "md.lammpstrj").write_text(DUMP + "ITEM: TIMESTEP\n20\n")
    calls = []
    record = {"id": "job", "terminal": False, "scheduler_state": "RUNNING"}

    async def remote(record, action, **kwargs):
        calls.append(action)
        return scientific_snapshot(tmp_path, kwargs["file"])

    chat = SimpleNamespace(
        plans=SimpleNamespace(slurm=SimpleNamespace(load=lambda *_: record, remote=remote))
    )
    node = {"id": "md", "kind": "task"}
    plan = {
        "revision": 1,
        "status": "running",
        "nodes": [node],
        "runtime": {"md": {"status": "running", "jobs": ["job"]}},
    }
    info = await inspect_generic_step(chat, "session", plan, node, "info", None)
    assert info["monitor"]["engine"] == "LAMMPS" and info["ionic_step"] == 20
    assert info["files"]["md.lammpstrj"] == "作业目录快照"
    result = await inspect_generic_step(chat, "session", plan, node, "md.lammpstrj", None)
    assert result["content"] == DUMP and calls == ["scientific_snapshot"] * 2


def test_minimization_and_normalized_energy_are_not_misreported_as_md():
    data = lammps_md(LOG + "minimize 0 0.1 200 2000\nStep Temp PotEng\n30 0 -100\n")
    assert data["stage"] == "minimization" and not data["md_points"]
    data = lammps_md(LOG, "thermo_modify norm yes\n")
    assert data["energy_unit"] == "eV/atom"
