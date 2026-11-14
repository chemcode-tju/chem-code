"""Complete VASP records, constrained forces and bounded live-file snapshots."""

import math

from chemistry_agent.automation.slurm_remote import (
    ionic_energy_points,
    selective_atoms,
    vasp_convergence,
    vasp_snapshot,
)

POSCAR = (
    "test\n1\n3 0 0\n0 3 0\n0 0 3\nCu\n2\nSelective dynamics\nDirect\n0 0 0 F F F\n.5 .5 .5 T T T\n"
)


def force_block(step: int, free_force: str = ".03 .04 0") -> str:
    return (
        f" Iteration {step}( 5)\n POSITION TOTAL-FORCE (eV/Angst)\n"
        f" ----------\n 0 0 0 9 0 0\n .5 .5 .5 {free_force}\n ----------\n"
    )


def test_constrained_force_and_actual_stopping_marker():
    result = vasp_convergence(
        force_block(1) + force_block(2, ".003 .004 0"),
        "NSW=100; IBRION=2\nEDIFFG=-0.01 # EDIFFG=-1",
        POSCAR,
    )
    assert result["force_points"] == [[1, 0.05], [2, 0.005]]
    assert result["force_basis"] == "free_atoms"
    assert result["nsw"] == 100 and result["ediffg"] == -0.01
    assert not result["ionic_converged"]  # A small final force alone isn't the VASP stopping event.
    stopped = vasp_convergence(
        force_block(2) + " reached required accuracy - stopping structural energy minimisation\n",
        "",
        POSCAR,
    )
    assert stopped["ionic_converged"]


def test_partial_directional_constraints_are_not_cartesian_masks():
    result = vasp_convergence(force_block(1), "", POSCAR.replace("F F F", "T F T"))
    assert result["force_basis"] == "raw_directional_constraints"
    assert result["force_points"][0][1] == 9
    assert selective_atoms("broken") == (None, "raw")


def test_truncated_rows_unknown_indices_and_nonfinite_values_are_not_data():
    assert not vasp_convergence(force_block(1).replace(" Iteration 1( 5)\n", ""), "", POSCAR)[
        "force_points"
    ]
    for tail in [" .5 .5 .5 .1 .2", " .5 .5 .5 nan .2 .3\n ----------\n", " ----------"]:
        output = (
            force_block(1)
            + " Iteration 2( 1)\n POSITION TOTAL-FORCE (eV/Angst)\n ----------\n 0 0 0 0 0 0\n"
            + tail
        )
        assert vasp_convergence(output, "", POSCAR)["force_points"] == [[1, 0.05]]
    assert vasp_convergence(force_block(1, "0 0 0"), "", POSCAR)["force_points"] == [[1, 0.0]]


def test_restart_and_fortran_energy_records():
    log = " 1 F= -1 E0= -1D+0\n 2 F= -2 E0= -2\n 1 F= -3 E0= -3\n 2 F= -4 E0= -4"
    assert ionic_energy_points(log) == [[1, -3.0]]
    result = vasp_convergence(force_block(8) + force_block(9) + force_block(1), "", POSCAR)
    assert result["force_points"] == [[1, 0.05]]


def test_snapshot_tail_is_compact_and_does_not_transmit_outcar(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chemistry_agent.automation.slurm_remote.VASP_OUTCAR_LIMIT", 4 * 1024 * 1024
    )
    (tmp_path / "POSCAR").write_text(POSCAR)
    (tmp_path / "INCAR").write_text("NSW=250\nEDIFFG=-0.02\n")
    (tmp_path / "OUTCAR").write_text("x\n" * (3 * 1024 * 1024) + force_block(120))
    result = vasp_snapshot(tmp_path)
    assert result["monitor"]["force_history_truncated"]
    assert result["monitor"]["force_points"] == [[120, 0.05]]
    assert "OUTCAR" not in result["contents"] and "POSCAR" not in result["contents"]
    assert math.isfinite(result["monitor"]["force_points"][0][1])
