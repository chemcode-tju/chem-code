"""Checkpointed VASP relaxation → convergence gate → static energy workflow."""

import math

from aiida import orm
from aiida.engine import ToContext, WorkChain, calcfunction
from aiida.plugins import CalculationFactory


def convergence_check(misc: dict, *, require_ionic: bool) -> tuple[bool, str]:
    """Fail closed on missing convergence evidence or non-finite numerical results."""
    status = misc.get("run_status", {})
    if status.get("electronic_converged") is not True:
        return False, "电子收敛未通过或缺少证据"
    if require_ionic and status.get("ionic_converged") is not True:
        return False, "结构收敛未通过或缺少证据"
    energy = misc.get("total_energies", {}).get("energy_extrapolated")
    if not isinstance(energy, (float, int)) or not math.isfinite(energy):
        return False, "缺少有限的总能量"
    forces = misc.get("forces")
    if not forces or any(
        len(vector) != 3 or not all(math.isfinite(v) for v in vector) for vector in forces
    ):
        return False, "缺少有效原子力"
    return True, "通过执行与收敛检查；尚未验证截断能或 k 点精度"


@calcfunction
def summarize_workflow(relaxation: orm.Dict, singlepoint: orm.Dict) -> orm.Dict:
    relaxed, final = relaxation.get_dict(), singlepoint.get_dict()
    return orm.Dict(
        dict={
            "relaxation_energy_ev": relaxed["total_energies"]["energy_extrapolated"],
            "singlepoint_energy_ev": final["total_energies"]["energy_extrapolated"],
            "max_force_ev_per_angstrom": max(
                math.sqrt(sum(v * v for v in vector)) for vector in final["forces"]
            ),
            "electronic_converged": True,
            "ionic_converged": True,
            "scientific_accuracy": "not_assessed",
        }
    )


class RelaxStaticWorkChain(WorkChain):
    """Approve the complete input plan once; AiiDA advances only after validation."""

    @classmethod
    def define(cls, spec) -> None:
        super().define(spec)
        calculation = CalculationFactory("vasp.vasp")
        spec.expose_inputs(calculation, namespace="relax")
        spec.expose_inputs(calculation, namespace="singlepoint")
        spec.output("structure", valid_type=orm.StructureData)
        spec.output("results", valid_type=orm.Dict)
        spec.output("summary", valid_type=orm.Dict)
        spec.exit_code(410, "RELAX_FAILED", message="结构优化未正常结束。")
        spec.exit_code(411, "RELAX_NOT_CONVERGED", message="优化未通过电子/结构收敛检查。")
        spec.exit_code(412, "STRUCTURE_MISSING", message="缺少优化后的结构，未启动单点计算。")
        spec.exit_code(420, "STATIC_FAILED", message="单点计算未正常结束。")
        spec.exit_code(421, "STATIC_NOT_CONVERGED", message="单点计算未通过收敛检查。")
        spec.outline(
            cls.run_relax, cls.inspect_relax, cls.run_static, cls.inspect_static, cls.finish
        )

    def run_relax(self):
        calculation = CalculationFactory("vasp.vasp")
        inputs = self.exposed_inputs(calculation, namespace="relax")
        inputs["metadata"]["label"] = f"{self.node.label}:relax"
        inputs["metadata"]["call_link_label"] = "relax"
        self.node.set_process_status("正在优化结构")
        return ToContext(relaxation=self.submit(calculation, **inputs))

    def inspect_relax(self):
        node = self.ctx.relaxation
        if not node.is_finished_ok:
            return self.exit_codes.RELAX_FAILED
        if (
            "misc" not in node.outputs
            or not convergence_check(node.outputs.misc.get_dict(), require_ionic=True)[0]
        ):
            return self.exit_codes.RELAX_NOT_CONVERGED
        if "structure" not in node.outputs:
            return self.exit_codes.STRUCTURE_MISSING
        self.ctx.optimized_structure = node.outputs.structure

    def run_static(self):
        calculation = CalculationFactory("vasp.vasp")
        inputs = self.exposed_inputs(calculation, namespace="singlepoint")
        inputs["structure"] = self.ctx.optimized_structure
        inputs["metadata"]["label"] = f"{self.node.label}:singlepoint"
        inputs["metadata"]["call_link_label"] = "singlepoint"
        self.node.set_process_status("优化收敛，正在计算单点能")
        return ToContext(singlepoint=self.submit(calculation, **inputs))

    def inspect_static(self):
        node = self.ctx.singlepoint
        if not node.is_finished_ok:
            return self.exit_codes.STATIC_FAILED
        if (
            "misc" not in node.outputs
            or not convergence_check(node.outputs.misc.get_dict(), require_ionic=False)[0]
        ):
            return self.exit_codes.STATIC_NOT_CONVERGED

    def finish(self) -> None:
        self.out("structure", self.ctx.optimized_structure)
        self.out("results", self.ctx.singlepoint.outputs.misc)
        self.out(
            "summary",
            summarize_workflow(self.ctx.relaxation.outputs.misc, self.ctx.singlepoint.outputs.misc),
        )
        self.node.set_process_status("优化与单点完成，结果已汇总")


@calcfunction
def cut_surface(structure: orm.StructureData, plan: orm.Dict) -> dict:
    from chemistry_agent.automation.constraints import positions_dof
    from chemistry_agent.automation.models import SurfacePlan
    from chemistry_agent.automation.surface import make_surface

    atoms = make_surface(structure.get_ase(), SurfacePlan.model_validate(plan.get_dict()))
    atoms.set_tags(0)  # Layer labels must not become Cu1/Cu2 pseudo-species.
    return {
        "structure": orm.StructureData(ase=atoms),
        "dynamics": orm.Dict(dict={"positions_dof": positions_dof(atoms)}),
    }


class BulkSurfaceWorkChain(WorkChain):
    """Persist the whole approved plan; the AiiDA daemon advances after each child."""

    @classmethod
    def define(cls, spec) -> None:
        from aiida.engine import if_

        super().define(spec)
        calculation = CalculationFactory("vasp.vasp")
        spec.expose_inputs(
            calculation,
            namespace="bulk",
            namespace_options={"required": False, "populate_defaults": False},
        )
        spec.expose_inputs(calculation, namespace="surface")
        spec.input("plan", valid_type=orm.Dict)
        spec.input("bulk_structure", valid_type=orm.StructureData, required=False)
        spec.input("bulk_results", valid_type=orm.Dict, required=False)
        spec.output("structure", valid_type=orm.StructureData)
        spec.output("dynamics", valid_type=orm.Dict)
        spec.output("results", valid_type=orm.Dict)
        spec.output("summary", valid_type=orm.Dict)
        spec.exit_code(430, "BULK_FAILED", message="体相未正常结束或未通过收敛检查。")
        spec.exit_code(431, "SURFACE_INVALID", message="无法从体相构建指定表面。")
        spec.exit_code(432, "SURFACE_FAILED", message="表面优化失败或未通过收敛检查。")
        spec.exit_code(433, "FIXED_ATOMS_MOVED", message="固定原子、晶胞或表面力检查未通过。")
        spec.outline(
            cls.initialize,
            if_(cls.needs_bulk)(cls.run_bulk, cls.inspect_bulk),
            cls.build_surface,
            cls.run_surface,
            cls.inspect_surface,
            cls.finish,
        )

    def initialize(self):
        if "bulk_structure" in self.inputs:
            if (
                "bulk_results" not in self.inputs
                or not convergence_check(self.inputs.bulk_results.get_dict(), require_ionic=True)[0]
            ):
                return self.exit_codes.BULK_FAILED
            self.ctx.bulk_structure = self.inputs.bulk_structure
        elif not self.inputs.get("bulk"):
            return self.exit_codes.BULK_FAILED

    def needs_bulk(self) -> bool:
        return "bulk_structure" not in self.inputs

    def run_bulk(self):
        calculation = CalculationFactory("vasp.vasp")
        inputs = self.exposed_inputs(calculation, namespace="bulk")
        inputs["metadata"].update(label=f"{self.node.label}:bulk", call_link_label="bulk")
        self.node.set_process_status("正在优化体相晶格")
        return ToContext(bulk_job=self.submit(calculation, **inputs))

    def inspect_bulk(self):
        node = self.ctx.bulk_job
        if (
            not node.is_finished_ok
            or "structure" not in node.outputs
            or "misc" not in node.outputs
            or not convergence_check(node.outputs.misc.get_dict(), require_ionic=True)[0]
        ):
            return self.exit_codes.BULK_FAILED
        self.ctx.bulk_structure = node.outputs.structure

    def build_surface(self):
        from chemistry_agent.errors import AgentError

        self.node.set_process_status("体相收敛，正在切表面并设置固定层")
        try:
            result = cut_surface(self.ctx.bulk_structure, self.inputs.plan)
        except (AgentError, ValueError) as exc:
            self.report(str(exc))
            return self.exit_codes.SURFACE_INVALID
        self.ctx.slab = result["structure"]
        self.ctx.dynamics = result["dynamics"]

    def run_surface(self):
        calculation = CalculationFactory("vasp.vasp")
        inputs = self.exposed_inputs(calculation, namespace="surface")
        inputs.update(structure=self.ctx.slab, dynamics=self.ctx.dynamics)
        inputs["metadata"].update(label=f"{self.node.label}:surface", call_link_label="surface")
        self.node.set_process_status("表面已构建，正在优化自由层")
        return ToContext(surface_job=self.submit(calculation, **inputs))

    def inspect_surface(self):
        import numpy as np
        from ase.geometry import find_mic

        node = self.ctx.surface_job
        if (
            not node.is_finished_ok
            or "structure" not in node.outputs
            or "misc" not in node.outputs
            or not convergence_check(node.outputs.misc.get_dict(), require_ionic=True)[0]
        ):
            return self.exit_codes.SURFACE_FAILED
        before, after = self.ctx.slab.get_ase(), node.outputs.structure.get_ase()
        if (
            len(before) != len(after)
            or before.get_chemical_symbols() != after.get_chemical_symbols()
            or not np.allclose(before.cell, after.cell, atol=1e-6, rtol=0)
        ):
            return self.exit_codes.FIXED_ATOMS_MOVED
        movable = np.array(self.ctx.dynamics.get_dict()["positions_dof"], dtype=bool)
        fixed = ~movable.any(axis=1)
        _, displacement = find_mic(after.positions - before.positions, before.cell, pbc=True)
        if np.any(displacement[fixed] > 1e-5):
            return self.exit_codes.FIXED_ATOMS_MOVED
        forces = np.asarray(node.outputs.misc.get_dict()["forces"])
        if forces.shape != (len(after), 3):
            return self.exit_codes.SURFACE_FAILED
        free_force = forces[movable.any(axis=1)]
        self.ctx.max_force = float(np.linalg.norm(free_force, axis=1).max())
        threshold = abs(self.inputs.surface.parameters.get_dict()["ediffg"])
        if self.ctx.max_force > threshold + 1e-5:
            return self.exit_codes.FIXED_ATOMS_MOVED

    def finish(self) -> None:
        result = self.ctx.surface_job.outputs.misc
        plan = self.inputs.plan.get_dict()
        self.out("structure", self.ctx.surface_job.outputs.structure)
        self.out("dynamics", self.ctx.dynamics)
        self.out("results", result)
        self.out(
            "summary",
            surface_summary(
                self.ctx.bulk_structure, result, self.inputs.plan, orm.Float(self.ctx.max_force)
            ),
        )
        self.node.set_process_status(
            f"{plan['layers']}层表面优化完成，固定底部{plan['fixed_layers']}层，结果已汇总"
        )


@calcfunction
def surface_summary(
    bulk: orm.StructureData, results: orm.Dict, plan: orm.Dict, max_force: orm.Float
) -> orm.Dict:
    data = plan.get_dict()
    return orm.Dict(
        dict={
            "lattice_constant_angstrom": float(bulk.get_ase().cell.lengths().mean()),
            "surface_energy_total_ev": results.get_dict()["total_energies"]["energy_extrapolated"],
            "max_free_force_ev_per_angstrom": max_force.value,
            "layers": data["layers"],
            "fixed_layers": data["fixed_layers"],
            "vacuum_angstrom": data["vacuum_angstrom"],
            "size": data["size"],
            "scientific_accuracy": "not_assessed",
        }
    )
