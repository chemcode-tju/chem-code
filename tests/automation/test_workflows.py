"""Exercise the actual AiiDA outline with local child processes, without VASP or SSH."""

import pytest

pytest.importorskip("aiida")
from aiida import orm  # noqa: E402
from aiida.engine import WorkChain, run_get_node  # noqa: E402
from aiida.tools.pytest_fixtures.configuration import (  # noqa: F401, E402
    aiida_config_factory,
    aiida_profile_factory,
)

from chemistry_agent.automation import workflows  # noqa: E402


class FakeVasp(WorkChain):
    @classmethod
    def define(cls, spec) -> None:
        super().define(spec)
        spec.input("structure", valid_type=orm.StructureData)
        spec.input("parameters", valid_type=orm.Dict)
        spec.output("structure", valid_type=orm.StructureData, required=False)
        spec.output("misc", valid_type=orm.Dict, required=False)
        spec.exit_code(10, "FAILED", message="injected failure")
        spec.outline(cls.calculate)

    def calculate(self):
        params = self.inputs.parameters.get_dict()
        if params.get("fail"):
            return self.exit_codes.FAILED
        structure = self.inputs.structure.clone()
        if params.get("relax"):
            structure.reset_sites_positions([[0.1, 0.2, 0.3]])
        self.out("structure", structure.store())
        self.out(
            "misc",
            orm.Dict(
                dict={
                    "run_status": {
                        "electronic_converged": True,
                        "ionic_converged": not params.get("unconverged"),
                    },
                    "total_energies": {"energy_extrapolated": -1.5},
                    "forces": [[0.001, 0.002, 0.003]],
                }
            ).store(),
        )


class TestOutline(workflows.RelaxStaticWorkChain):
    __test__ = False


@pytest.fixture
def profile(tmp_path, aiida_config_factory, aiida_profile_factory, monkeypatch):  # noqa: F811
    with aiida_config_factory(tmp_path) as config, aiida_profile_factory(config) as profile:
        monkeypatch.setattr(workflows, "CalculationFactory", lambda name: FakeVasp)
        yield profile


@pytest.mark.parametrize(
    "unconverged,static_fail,exit_code", [(False, False, 0), (True, False, 411), (False, True, 420)]
)
def test_real_outline_gates_and_uses_optimized_structure(
    profile, unconverged, static_fail, exit_code
):
    structure = orm.StructureData(cell=[[4, 0, 0], [0, 4, 0], [0, 0, 4]])
    structure.append_atom(position=(0, 0, 0), symbols="Cu")
    outputs, node = run_get_node(
        TestOutline,
        relax={
            "structure": structure,
            "parameters": orm.Dict(dict={"relax": True, "unconverged": unconverged}),
        },
        singlepoint={"structure": structure, "parameters": orm.Dict(dict={"fail": static_fail})},
        metadata={"label": "local-test"},
    )
    assert node.exit_status == exit_code
    children = sorted(
        [child for child in node.called if isinstance(child, orm.WorkChainNode)],
        key=lambda child: child.pk,
    )
    assert len(children) == (1 if unconverged else 2)
    if not unconverged:
        assert children[1].inputs.structure.uuid == children[0].outputs.structure.uuid
        assert children[1].inputs.structure.sites[0].position == (0.1, 0.2, 0.3)
    if exit_code == 0:
        assert outputs["summary"].get_dict()["singlepoint_energy_ev"] == -1.5
        assert outputs["summary"].get_dict()["scientific_accuracy"] == "not_assessed"
    else:
        assert "summary" not in outputs


@pytest.mark.parametrize(
    "misc",
    [
        {},
        {
            "run_status": {"electronic_converged": True},
            "total_energies": {"energy_extrapolated": float("nan")},
        },
    ],
)
def test_missing_scientific_evidence_does_not_pass(misc):
    assert not workflows.convergence_check(misc, require_ionic=True)[0]
