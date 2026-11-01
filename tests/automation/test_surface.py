"""Constraint fidelity and the real AiiDA surface outline, without SSH or VASP execution."""

import io
import sys

import numpy as np
import pytest
from ase.build import bulk
from ase.constraints import FixAtoms, FixBondLengths, FixCartesian, FixScaled
from ase.io import read, write

pytest.importorskip("aiida")
from aiida import orm  # noqa: E402
from aiida.engine import WorkChain, run_get_node  # noqa: E402
from aiida.tools.pytest_fixtures.configuration import (  # noqa: F401, E402
    aiida_config_factory,
    aiida_profile_factory,
)
from aiida_vasp.parsers.content_parsers.poscar import PoscarParser  # noqa: E402

from chemistry_agent.automation import workflows  # noqa: E402
from chemistry_agent.automation.backend import read_structure  # noqa: E402
from chemistry_agent.automation.constraints import positions_dof, structure_text  # noqa: E402
from chemistry_agent.automation.models import StructureInput, SurfacePlan  # noqa: E402
from chemistry_agent.automation.surface import make_surface  # noqa: E402
from chemistry_agent.errors import AgentError  # noqa: E402


def test_constraints_round_trip_union_and_vasp_coordinate_semantics():
    atoms = bulk("Cu", "fcc", a=3.6, cubic=True)
    atoms.set_constraint(
        [
            FixAtoms(indices=[0]),
            FixScaled([1, 2], mask=[True, False, True]),
            FixScaled(1, mask=[False, True, False]),
        ]
    )
    spec = StructureInput(content=structure_text(atoms), format="extxyz")
    recovered = read_structure(spec)
    assert positions_dof(recovered) == [[False] * 3, [False] * 3, [False, True, False], [True] * 3]
    atoms = bulk("Cu", "fcc", a=3.6)
    atoms.set_constraint(FixCartesian(0, mask=[True, False, False]))
    with pytest.raises(AgentError, match="等价"):
        positions_dof(atoms)
    atoms.set_constraint(FixBondLengths([[0, 0]]))
    with pytest.raises(AgentError, match="FixBondLengths"):
        positions_dof(atoms)


def test_ase_poscar_fixed_layers_and_total_vacuum():
    slab = make_surface(bulk("Cu", "fcc", a=3.63689555, cubic=True), SurfacePlan())
    assert len(slab) == 16
    assert slab.cell[0, 0] == pytest.approx(3.63689555 * np.sqrt(2))
    assert slab.cell[2, 2] - np.ptp(slab.positions[:, 2]) == pytest.approx(20)
    flags = np.array(positions_dof(slab))
    assert (~flags.any(axis=1)).sum() == 8
    assert np.all(
        slab.positions[~flags.any(axis=1), 2] < slab.positions[flags.any(axis=1), 2].min()
    )
    text = io.StringIO()
    write(text, slab, format="vasp", direct=True)
    restored = read_structure(StructureInput(content=text.getvalue()))
    assert positions_dof(restored) == flags.tolist()


class FakeSurfaceVasp(WorkChain):
    @classmethod
    def define(cls, spec) -> None:
        super().define(spec)
        spec.input("structure", valid_type=orm.StructureData)
        spec.input("parameters", valid_type=orm.Dict)
        spec.input("dynamics", valid_type=orm.Dict, required=False)
        spec.output("structure", valid_type=orm.StructureData)
        spec.output("misc", valid_type=orm.Dict)
        spec.outline(cls.calculate)

    def calculate(self):
        atoms = self.inputs.structure.get_ase()
        params = self.inputs.parameters.get_dict()
        if len(atoms) == 4:
            atoms.set_cell([3.7] * 3, scale_atoms=True)
        if params.get("move_fixed"):
            atoms.positions[0, 2] += 0.1
        self.out("structure", orm.StructureData(ase=atoms).store())
        self.out(
            "misc",
            orm.Dict(
                dict={
                    "run_status": {
                        "electronic_converged": True,
                        "ionic_converged": not params.get("unconverged"),
                    },
                    "total_energies": {"energy_extrapolated": -len(atoms)},
                    "forces": [[0, 0, 0.001]] * len(atoms),
                }
            ).store(),
        )


class SurfaceOutline(workflows.BulkSurfaceWorkChain):
    __test__ = False


@pytest.fixture
def profile(tmp_path, aiida_config_factory, aiida_profile_factory, monkeypatch):  # noqa: F811
    with aiida_config_factory(tmp_path) as config, aiida_profile_factory(config) as profile:
        monkeypatch.setattr(workflows, "CalculationFactory", lambda name: FakeSurfaceVasp)
        yield profile


@pytest.mark.parametrize(
    "reuse,unconverged,move_fixed,expected",
    [
        (False, False, False, 0),
        (True, False, False, 0),
        (False, True, False, 430),
        (False, False, True, 433),
    ],
)
def test_surface_outline_uses_final_lattice_and_preserves_fixed_atoms(
    profile, reuse, unconverged, move_fixed, expected
):
    structure = orm.StructureData(ase=bulk("Cu", "fcc", a=3.6, cubic=True))
    inputs = {
        "plan": orm.Dict(dict=SurfacePlan().model_dump(mode="json")),
        "surface": {
            "structure": structure,
            "parameters": orm.Dict(dict={"ediffg": -0.03, "move_fixed": move_fixed}),
        },
    }
    if reuse:
        previous, previous_node = run_get_node(
            FakeSurfaceVasp, structure=structure, parameters=orm.Dict(dict={})
        )
        inputs.update(bulk_structure=previous["structure"], bulk_results=previous["misc"])
    else:
        inputs["bulk"] = {
            "structure": structure,
            "parameters": orm.Dict(dict={"unconverged": unconverged}),
        }
    outputs, node = run_get_node(SurfaceOutline, **inputs, metadata={"label": "surface-test"})
    assert node.exit_status == expected
    children = [child for child in node.called if isinstance(child, orm.WorkChainNode)]
    assert len(children) == (1 if reuse or unconverged else 2)
    if not unconverged:
        surface = next(child for child in children if child.label.endswith(":surface"))
        atoms = surface.inputs.structure.get_ase()
        assert len(atoms) == 16
        assert atoms.cell[0, 0] == pytest.approx(3.7 * np.sqrt(2))
        assert sum(not any(row) for row in surface.inputs.dynamics.get_dict()["positions_dof"]) == 8
    if expected == 0:
        summary = outputs["summary"].get_dict()
        assert summary["lattice_constant_angstrom"] == pytest.approx(3.7)
        assert summary["scientific_accuracy"] == "not_assessed"


def test_real_aiida_vasp_poscar_retains_fixed_atoms(profile, tmp_path):
    atoms = make_surface(bulk("Cu", "fcc", a=3.6, cubic=True), SurfacePlan())
    atoms.set_tags(0)
    path = tmp_path / "POSCAR"
    PoscarParser(
        data=orm.StructureData(ase=atoms), options={"positions_dof": positions_dof(atoms)}
    ).write(str(path))
    restored = read(path, format="vasp")
    assert positions_dof(restored) == positions_dof(atoms)


def test_result_import_preserves_final_structure_and_constraints(profile):
    from chemistry_agent.automation.surface import job_structure

    structure = orm.StructureData(ase=bulk("Cu", "fcc", a=3.6, cubic=True))
    _, node = run_get_node(
        SurfaceOutline,
        plan=orm.Dict(dict=SurfacePlan().model_dump(mode="json")),
        bulk={"structure": structure, "parameters": orm.Dict(dict={})},
        surface={"structure": structure, "parameters": orm.Dict(dict={"ediffg": -0.03})},
        metadata={"label": "chem-agent:surface-import-test"},
    )
    exported = job_structure({"job_id": str(node.pk)})
    atoms = read_structure(StructureInput.model_validate(exported["structure"]))
    assert len(atoms) == 16
    assert sum(not any(row) for row in positions_dof(atoms)) == 8
    assert exported["source_structure_uuid"] == node.outputs.structure.uuid


@pytest.mark.asyncio
async def test_result_import_rejects_other_session_before_worker(settings_factory):
    from uuid import uuid4

    from chemistry_agent.automation.chat import CalculationChat, JobInput
    from chemistry_agent.automation.service import AutomationService
    from chemistry_agent.tools import ToolContext

    chat = CalculationChat(AutomationService(settings_factory(automation_enabled=True)))

    async def forbidden(*args, **kwargs):
        pytest.fail("unowned job must not reach the worker")

    chat.service.call = forbidden
    with pytest.raises(AgentError, match="当前对话"):
        await chat.import_job_structure(
            JobInput(job_id="60"), ToolContext(str(uuid4()), str(uuid4()))
        )


def test_rotated_cartesian_constraints_export_as_fractional_flags():
    from chemistry_agent.automation.constraints import poscar_text

    atoms = bulk("Cu", "fcc", a=3.6, cubic=True)
    atoms.set_cell([[0, 3.6, 0], [3.6, 0, 0], [0, 0, -3.6]], scale_atoms=True)
    atoms.set_constraint(FixCartesian(0, mask=[True, False, False]))
    exported = read(io.StringIO(poscar_text(atoms)), format="vasp")
    assert positions_dof(exported)[0] == [True, False, True]


@pytest.mark.asyncio
async def test_real_sandbox_can_write_fixed_surface_poscar(tmp_path):
    import os
    import sys

    from chemistry_agent.automation.modeling import execute_model

    if sys.platform != "darwin" or os.getenv("CHEM_SANDBOX_TEST") != "1":
        pytest.skip("Requires explicit macOS sandbox integration run")
    code = """from ase.build import fcc100
from ase.constraints import FixAtoms
from ase.io import write
# Configuration
lattice_constant = 3.63689555

def main():
    atoms = fcc100("Cu", size=(2, 2, 4), a=lattice_constant, vacuum=10)
    atoms.pbc = True
    atoms.set_constraint(FixAtoms(mask=atoms.get_tags() >= 3))
    write("structure.vasp", atoms, format="vasp", direct=True)

if __name__ == "__main__":
    main()
"""
    text = await execute_model(tmp_path, code, output_format="vasp")
    atoms = read(io.StringIO(text), format="vasp")
    assert len(atoms) == 16
    assert sum(not any(row) for row in positions_dof(atoms)) == 8


def test_co_workflow_parallel_outputs_and_branch_only_repair(profile, monkeypatch) -> None:
    from chemistry_agent.automation import adsorption

    class CoTestOutline(adsorption.CoAdsorptionWorkChain):
        pass

    # AiiDA persists process classes by importable name.
    CoTestOutline.__qualname__ = "CoTestOutline"
    monkeypatch.setattr(sys.modules[__name__], "CoTestOutline", CoTestOutline, raising=False)
    monkeypatch.setattr(adsorption, "CalculationFactory", lambda _: FakeSurfaceVasp)
    bulk_structure = orm.StructureData(ase=bulk("Cu", "fcc", a=3.6, cubic=True))
    plan = SurfacePlan(co_sites=["top", "bridge", "hollow"])
    default = {"structure": bulk_structure, "parameters": orm.Dict(dict={"ediffg": -0.03})}
    inputs = {key: dict(default) for key in ["bulk", "surface", "top", "bridge", "hollow", "gas"]}
    inputs["gas"]["structure"] = orm.StructureData(ase=adsorption.gas_co(plan))
    inputs["bridge"]["parameters"] = orm.Dict(dict={"unconverged": True})
    inputs["plan"] = orm.Dict(dict=plan.model_dump(mode="json"))
    outputs, parent = run_get_node(CoTestOutline, **inputs, metadata={"label": "co-test"})
    assert parent.exit_status == 434
    children = {
        node.label.split(":")[-1]: node
        for node in parent.called
        if isinstance(node, orm.WorkChainNode)
    }
    assert set(children) == {"bulk", "surface", "gas", "top", "bridge", "hollow"}
    cut = next(node for node in parent.called if node.process_label == "cut_surface")
    assert cut.inputs.structure.uuid == children["bulk"].outputs.structure.uuid
    assert children["surface"].inputs.structure.uuid == cut.outputs.structure.uuid
    for key in plan.co_sites:
        builder = children[key].inputs.structure.creator
        assert builder.inputs.site.value == key
        assert builder.inputs.slab.uuid == children["surface"].outputs.structure.uuid
        atoms = children[key].inputs.structure.get_ase()
        assert atoms.get_chemical_symbols() == ["Cu"] * 16 + ["C", "O"]
        assert atoms.cell[0, 0] == pytest.approx(3.7 * np.sqrt(2))
        assert (
            sum(not any(row) for row in children[key].inputs.dynamics.get_dict()["positions_dof"])
            == 8
        )
    assert (
        len({tuple(children[k].inputs.structure.sites[-2].position[:2]) for k in plan.co_sites})
        == 3
    )
    reuse = {key: outputs[key] for key in ("structure", "results", "summary", "dynamics")}
    reuse.update({key: children[key].outputs.misc for key in ("top", "hollow", "gas")})
    inputs["bridge"] = default
    final, retry = run_get_node(CoTestOutline, **inputs, reuse=reuse, metadata={"label": "repair"})
    assert retry.is_finished_ok
    assert [node.label for node in retry.called if isinstance(node, orm.WorkChainNode)] == [
        "repair:bridge"
    ]
    assert final["adsorption"].get_dict()["adsorption_ev"] == dict.fromkeys(plan.co_sites, 0)
