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
