"""CO adsorption on a relaxed fcc(100) slab, referenced to gas CO and the clean slab."""

import numpy as np
from aiida import orm
from aiida.engine import ToContext, calcfunction
from aiida.plugins import CalculationFactory
from ase import Atoms

from chemistry_agent.automation.constraints import positions_dof
from chemistry_agent.automation.models import SurfacePlan
from chemistry_agent.automation.surface import restore_dynamics
from chemistry_agent.automation.workflows import BulkSurfaceWorkChain, convergence_check


def gas_co(plan: SurfacePlan) -> Atoms:
    atoms = Atoms(
        "CO", positions=[[0, 0, 0], [0, 0, plan.co_bond]], cell=[plan.gas_cell] * 3, pbc=True
    )
    atoms.center()
    return atoms


def decorate_co(slab: Atoms, plan: SurfacePlan, site: str) -> Atoms:
    """Anchor C above a top atom, neighbouring pair midpoint or square centre."""
    atoms = slab.copy()
    fractional = atoms.get_scaled_positions()
    layer_size = plan.size[0] * plan.size[1]
    top = np.argsort(atoms.positions[:, 2])[-layer_size:]
    # Choose nearest top-layer sites to reference fractional grid points, respecting PBC.
    anchor = top[np.argmin(np.linalg.norm(fractional[top, :2] % 1, axis=1))]
    origin = fractional[anchor, :2]
    targets = {
        "top": [(0, 0)],
        "bridge": [(0, 0), (1, 0)],
        "hollow": [(0, 0), (1, 0), (0, 1), (1, 1)],
    }[site]
    points = []
    for offset in targets:
        target = origin + np.array(offset) / plan.size
        delta = fractional[top, :2] - target
        delta -= np.rint(delta)
        index = np.argmin(np.linalg.norm(delta @ atoms.cell[:2], axis=1))
        position = (target + delta[index]) @ atoms.cell[:2]
        position[2] = atoms.positions[top[index], 2]
        points.append(position)
    carbon = np.mean(points, axis=0) + [0, 0, plan.co_height]
    atoms += Atoms("CO", positions=[carbon, carbon + [0, 0, plan.co_bond]])
    atoms.wrap()
    return atoms


@calcfunction
def initial_adsorbate(
    slab: orm.StructureData, dynamics: orm.Dict, plan: orm.Dict, site: orm.Str
) -> dict:
    atoms = slab.get_ase()
    restore_dynamics(atoms, dynamics.get_dict()["positions_dof"])
    atoms = decorate_co(atoms, SurfacePlan.model_validate(plan.get_dict()), site.value)
    return {
        "structure": orm.StructureData(ase=atoms),
        "dynamics": orm.Dict(dict={"positions_dof": positions_dof(atoms)}),
    }


@calcfunction
def adsorption_energies(slab: orm.Dict, gas: orm.Dict, **sites: orm.Dict) -> orm.Dict:
    def energy(result: orm.Dict) -> float:
        return result.get_dict()["total_energies"]["energy_extrapolated"]

    return orm.Dict(
        dict={
            "clean_slab_ev": energy(slab),
            "gas_co_ev": energy(gas),
            "adsorption_ev": {
                site: energy(result) - energy(slab) - energy(gas) for site, result in sites.items()
            },
        }
    )


def valid_branch(node: orm.ProcessNode) -> bool:
    """Require a converged relaxation with the approved cell, fixed atoms and force limit."""
    from ase.geometry import find_mic

    if not node.is_finished_ok or "structure" not in node.outputs or "misc" not in node.outputs:
        return False
    misc = node.outputs.misc.get_dict()
    if not convergence_check(misc, require_ionic=True)[0]:
        return False
    before, after = node.inputs.structure.get_ase(), node.outputs.structure.get_ase()
    if before.get_chemical_symbols() != after.get_chemical_symbols() or not np.allclose(
        before.cell, after.cell, atol=1e-6, rtol=0
    ):
        return False
    movable = np.ones(len(after), dtype=bool)
    if "dynamics" in node.inputs:
        movable = np.asarray(node.inputs.dynamics.get_dict()["positions_dof"]).any(axis=1)
        _, distances = find_mic(after.positions - before.positions, before.cell, pbc=True)
        if (distances[~movable] > 1e-5).any():
            return False
    forces = np.asarray(misc["forces"])
    threshold = node.inputs.parameters.get_dict().get("ediffg", 0)
    return (
        forces.shape == (len(after), 3)
        and threshold < 0
        and bool(np.all(np.linalg.norm(forces[movable], axis=1) <= -threshold + 1e-5))
    )


class CoAdsorptionWorkChain(BulkSurfaceWorkChain):
    @classmethod
    def define(cls, spec) -> None:
        from aiida.engine import if_

        super().define(spec)
        for name in ("gas", "top", "bridge", "hollow"):
            spec.expose_inputs(
                CalculationFactory("vasp.vasp"),
                namespace=name,
                namespace_options={"required": False, "populate_defaults": False},
            )
        spec.input_namespace("reuse", dynamic=True, valid_type=orm.Data, required=False)
        spec.output("adsorption", valid_type=orm.Dict)
        spec.exit_code(434, "ADSORPTION_FAILED", message="气相 CO 或吸附位点未通过优化检查。")
        spec.outline(
            if_(cls.needs_clean)(
                cls.initialize,
                if_(cls.needs_bulk)(cls.run_bulk, cls.inspect_bulk),
                cls.build_surface,
                cls.run_surface,
                cls.inspect_surface,
            ),
            cls.publish_clean,
            cls.run_branches,
            cls.inspect_branches,
            cls.finish_adsorption,
        )

    def needs_clean(self) -> bool:
        return "reuse" not in self.inputs or "structure" not in self.inputs.reuse

    def publish_clean(self) -> None:
        if self.needs_clean():
            self.finish()
            self.ctx.clean = self.ctx.surface_job.outputs.structure
            self.ctx.clean_energy = self.ctx.surface_job.outputs.misc
        else:
            for key in ("structure", "results", "summary", "dynamics"):
                self.out(key, self.inputs.reuse[key])
            self.ctx.clean = self.inputs.reuse.structure
            self.ctx.clean_energy = self.inputs.reuse.results
            self.ctx.dynamics = self.inputs.reuse.dynamics

    def run_branches(self):
        calc = CalculationFactory("vasp.vasp")
        pending = {}
        for key in ["gas", *self.inputs.plan.get_dict()["co_sites"]]:
            if key in self.inputs.get("reuse", {}):
                continue
            inputs = self.exposed_inputs(calc, namespace=key)
            if key != "gas":
                inputs.update(
                    initial_adsorbate(
                        self.ctx.clean,
                        self.ctx.dynamics,
                        self.inputs.plan,
                        orm.Str(key),
                    )
                )
            inputs["metadata"].update(label=f"{self.node.label}:{key}", call_link_label=key)
            pending[key] = self.submit(calc, **inputs)
        self.node.set_process_status("气相 CO 与各吸附位点已分别提交，等待并行结果")
        return ToContext(**pending)

    def inspect_branches(self):
        for key in ["gas", *self.inputs.plan.get_dict()["co_sites"]]:
            if key in self.inputs.get("reuse", {}):
                continue
            node = self.ctx[key]
            if not valid_branch(node):
                self.report(f"{key} 未通过检查：#{node.pk}")
                return self.exit_codes.ADSORPTION_FAILED

    def finish_adsorption(self) -> None:
        def result(key: str) -> orm.Dict:
            if key in self.inputs.get("reuse", {}):
                return self.inputs.reuse[key]
            return self.ctx[key].outputs.misc

        self.out(
            "adsorption",
            adsorption_energies(
                self.ctx.clean_energy,
                result("gas"),
                **{key: result(key) for key in self.inputs.plan.get_dict()["co_sites"]},
            ),
        )
        self.node.set_process_status("各初始位点的 CO 吸附能已汇总")
