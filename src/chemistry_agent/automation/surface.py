"""Preview and provenance-preserving preparation of conventional fcc bulk → (100) slabs."""

import hashlib
import json
from uuid import uuid4

import numpy as np
from aiida import orm
from ase import Atoms
from ase.build import fcc100
from ase.constraints import FixAtoms, FixScaled

from chemistry_agent.automation.constraints import positions_dof, structure_text
from chemistry_agent.automation.models import PreviewRequest, StructureInput, SurfacePlan
from chemistry_agent.errors import AgentError


def make_surface(bulk: Atoms, plan: SurfacePlan) -> Atoms:
    """Use the optimized conventional cubic cell; vacuum is the TOTAL periodic gap."""
    lengths = bulk.cell.lengths()
    if len(bulk) != 4 or len(set(bulk.get_chemical_symbols())) != 1:
        raise AgentError(
            "surface_bulk_invalid",
            "fcc100 流程需要单元素、4原子的 fcc 惯用胞。其他晶型请用 ASE 建模工具。",
            422,
        )
    if not np.allclose(lengths, lengths[0], rtol=1e-4) or not np.allclose(
        bulk.cell.angles(), 90, atol=1e-3
    ):
        raise AgentError(
            "surface_bulk_invalid", "体相输出不是立方惯用胞，不能直接作为 fcc 晶格常数。", 422
        )
    ideal = np.array([[0, 0, 0], [0, 0.5, 0.5], [0.5, 0, 0.5], [0.5, 0.5, 0]])
    scaled = bulk.get_scaled_positions()
    relative = (scaled - scaled[0]) % 1
    for target in ideal:
        delta = relative - target
        delta -= np.rint(delta)
        if np.min(np.linalg.norm(delta, axis=1)) > 1e-3:
            raise AgentError("surface_bulk_invalid", "体相原子位置不符合 fcc 惯用胞。", 422)
    slab = fcc100(
        bulk[0].symbol,
        size=(*plan.size, plan.layers),
        a=float(lengths.mean()),
        vacuum=plan.vacuum_angstrom / 2,
    )
    slab.pbc = True
    heights = np.unique(np.round(slab.positions[:, 2], 8))
    if len(heights) != plan.layers:
        raise AgentError("surface_layers_invalid", "表面层数检查失败。", 422)
    if plan.fixed_layers:
        fixed = slab.positions[:, 2] <= heights[plan.fixed_layers - 1] + 1e-6
        slab.set_constraint(FixAtoms(mask=fixed))
    return slab


def completed_bulk(identifier: str) -> orm.CalcJobNode:
    from chemistry_agent.automation.backend import job_details
    from chemistry_agent.automation.workflows import convergence_check

    record = job_details({"job_id": identifier})
    node = orm.load_node(record["id"])
    if (
        not isinstance(node, orm.CalcJobNode)
        or not node.is_finished_ok
        or "structure" not in node.outputs
        or "misc" not in node.outputs
    ):
        raise AgentError("bulk_not_ready", "复用体相任务须正常结束并具有最终结构和结果。", 422)
    if not convergence_check(node.outputs.misc.get_dict(), require_ionic=True)[0]:
        raise AgentError("bulk_not_converged", "体相尚未通过收敛检查。", 422)
    parameters = node.inputs.parameters.get_dict()
    if parameters.get("isif") not in {3, 7} or parameters.get("nsw", 0) < 1:
        raise AgentError(
            "bulk_not_lattice_optimization", "该任务没有优化晶胞，不能用作已优化晶格。", 422
        )
    return node


def restore_dynamics(atoms: Atoms, dof: list[list[bool]]) -> None:
    if len(dof) != len(atoms):
        raise AgentError("constraint_shape", "输出结构与固定原子标记数量不一致。", 422)
    constraints = [
        FixScaled(index, mask=~np.array(flags, dtype=bool))
        for index, flags in enumerate(dof)
        if any(flags) and not all(flags)
    ]
    fixed = [index for index, flags in enumerate(dof) if not any(flags)]
    if fixed:
        constraints.append(FixAtoms(indices=fixed))
    atoms.set_constraint(constraints)


def job_structure(payload: dict) -> dict:
    """Expose available geometry independently of job success or convergence."""
    from chemistry_agent.automation.backend import load_job, read_structure, validate_structure
    from chemistry_agent.automation.step_data import step_data

    node = load_job(payload["job_id"])
    source_uuid = None
    source_file = None
    if "structure" in node.outputs:
        atoms = node.outputs.structure.get_ase()
        source_uuid = node.outputs.structure.uuid
    elif isinstance(node, orm.CalcJobNode):
        snapshot = step_data({"root_id": node.uuid, "stage": "single", "file": "CONTCAR"})
        atoms = read_structure(StructureInput(format="vasp", content=snapshot["content"]))
        source_file = "CONTCAR"
    else:
        raise AgentError(
            "output_structure_missing", "此流程尚无输出结构，请选择具体计算步骤读取 CONTCAR。", 404
        )
    dynamics = node.outputs.dynamics if "dynamics" in node.outputs else None
    if dynamics is None and "dynamics" in node.inputs:
        dynamics = node.inputs.dynamics
    if dynamics is None and "relax" in node.inputs and "dynamics" in node.inputs.relax:
        dynamics = node.inputs.relax.dynamics
    if dynamics is not None:
        restore_dynamics(atoms, dynamics.get_dict()["positions_dof"])
    data = validate_structure({"format": "extxyz", "content": structure_text(atoms)})
    return {
        **data,
        "source_job_id": node.uuid,
        "source_job_pk": node.pk,
        "source_structure_uuid": source_uuid,
        "source_file": source_file,
        "source_process_state": node.process_state.value if node.process_state else None,
        "source_exit_status": node.exit_status,
        "source_finished_ok": node.is_finished_ok,
        "source_terminal": node.is_terminated,
        "source_run_status": (
            node.outputs.misc.get_dict().get("run_status", {}) if "misc" in node.outputs else {}
        ),
    }


def validate_surface_incar(incar: dict) -> None:
    if (
        incar.get("ISIF") != 2
        or incar.get("NSW", 0) < 1
        or incar.get("IBRION", -1) not in {1, 2}
        or incar.get("EDIFFG", 0) >= 0
    ):
        raise AgentError(
            "surface_relaxation_invalid",
            "表面优化须设置 ISIF=2、IBRION=1或2、NSW>0、EDIFFG<0，保留晶格与真空。",
            422,
        )


def preview_surface(body: PreviewRequest) -> dict:
    from chemistry_agent.automation import backend

    plan = body.surface_plan
    if plan is None or body.kind != "vasp":
        raise AgentError("surface_plan_missing", "请提供 VASP 表面流程参数。", 422)
    bulk_incar = {key.upper(): value for key, value in body.incar.items()}
    if bulk_incar.get("ISIF") not in {3, 7} or bulk_incar.get("NSW", 0) < 1:
        raise AgentError("bulk_relaxation_invalid", "体相阶段须优化晶胞：ISIF=3或7，NSW>0。", 422)
    bulk_atoms = backend.read_structure(body.structure)
    source = completed_bulk(plan.bulk_job_id) if plan.bulk_job_id else None
    if source is not None:
        bulk_atoms = source.outputs.structure.get_ase()
    slab = make_surface(bulk_atoms, plan)
    requests, stages, files = {}, [], {}
    base = body.model_dump(mode="json")
    base.update(workflow="single", surface_plan=None, incar=bulk_incar)
    if source is None:
        result = backend.preview(base)
        requests["bulk"] = base
        files.update({f"bulk/{name}": content for name, content in result["files"].items()})
        stages.append(
            {"stage": "bulk", "title": "体相晶格优化", "incar": bulk_incar, "kmesh": base["kmesh"]}
        )
    else:
        stages.append(
            {
                "stage": "bulk",
                "title": "复用已收敛体相",
                "reused_pk": source.pk,
                "source_uuid": source.uuid,
                "incar": source.inputs.parameters.get_dict(),
                "lattice_constant_angstrom": float(bulk_atoms.cell.lengths().mean()),
            }
        )
    surface_incar = {**bulk_incar, **{key.upper(): value for key, value in plan.incar.items()}}
    validate_surface_incar(surface_incar)
    child = {
        **base,
        "incar": surface_incar,
        "kmesh": list(plan.kmesh),
        "structure": {"format": "extxyz", "content": structure_text(slab)},
    }
    result = backend.preview(child)
    requests["surface"] = child
    files.update({f"surface/{name}": content for name, content in result["files"].items()})
    stages.append(
        {
            "stage": "surface",
            "title": "切表面并优化",
            "incar": surface_incar,
            "kmesh": list(plan.kmesh),
            "structure_source": "converged_bulk",
            "fixed_atoms": sum(not any(row) for row in positions_dof(slab)),
        }
    )
    if plan.co_sites:
        from chemistry_agent.automation.adsorption import decorate_co, gas_co

        if source and set(bulk_atoms.get_chemical_symbols()) != set(
            backend.read_structure(body.structure).get_chemical_symbols()
        ):
            raise AgentError("bulk_element_mismatch", "复用体相与请求元素不一致。", 422)
        for key in ["gas", *plan.co_sites]:
            atoms = gas_co(plan) if key == "gas" else decorate_co(slab, plan, key)
            incar = dict(surface_incar)
            if key == "gas":
                incar.update(ISMEAR=0, SIGMA=0.05, ISYM=0)
            if key in plan.branch_settings:
                incar.update(
                    {
                        k.upper(): v
                        for k, v in plan.branch_settings[key].model_dump(exclude_none=True).items()
                    }
                )
            branch = {
                **child,
                "incar": incar,
                "kmesh": [1, 1, 1] if key == "gas" else child["kmesh"],
                "structure": {"format": "extxyz", "content": structure_text(atoms)},
            }
            dry = backend.preview(branch)
            requests[key] = branch
            files.update({f"{key}/{name}": content for name, content in dry["files"].items()})
            stages.append(
                {
                    "stage": key,
                    "title": "气相 CO 参照" if key == "gas" else f"CO · {key}",
                    "incar": incar,
                    "kmesh": branch["kmesh"],
                    "parallel": True,
                }
            )
        stages.append(
            {"stage": "energies", "title": "汇总吸附能", "formula": "E(slab+CO) − E(slab) − E(CO)"}
        )
    identifier = str(uuid4())
    record = {
        **result,
        "preview_id": identifier,
        "created_at": backend.now(),
        "workflow": "bulk_surface",
        "request": body.model_dump(mode="json"),
        "stage_requests": requests,
        "stages": stages,
        "surface_plan": plan.model_dump(mode="json"),
        "bulk_source_uuid": source.uuid if source else None,
        "files": files,
        "max_calculations": len(requests),
        "total_wallclock_limit_seconds": len(requests) * body.resources.wallclock_seconds,
        "workflow_note": (
            f"体相收敛 → fcc(100) {plan.size[0]}×{plan.size[1]}、{plan.layers}层、"
            f"固定底部{plan.fixed_layers}层、总真空间隔{plan.vacuum_angstrom:g} Å → 表面优化。"
        )
        + (
            f"复用 AiiDA #{source.pk}，不重复计算体相。"
            if source
            else "表面 POSCAR 为初始晶格示意；执行时严格使用体相收敛晶格重新生成。"
        ),
    }
    _, fingerprint = surface_builder(record, backend.PREFIX + identifier)
    record["execution_fingerprint"] = fingerprint
    record["input_sha256"] = hashlib.sha256(
        json.dumps(
            {"files": files, "plan": record["surface_plan"], "source": record["bulk_source_uuid"]},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = backend.automation_directory() / identifier
    directory.mkdir(mode=0o700)
    (directory / "preview.json").write_text(json.dumps(record, ensure_ascii=False))
    return {key: value for key, value in record.items() if key not in {"request", "stage_requests"}}


def surface_builder(record: dict, label: str):
    from chemistry_agent.automation import backend
    from chemistry_agent.automation.workflows import BulkSurfaceWorkChain

    if record["surface_plan"].get("co_sites"):
        from chemistry_agent.automation.adsorption import CoAdsorptionWorkChain

        builder = CoAdsorptionWorkChain.get_builder()
    else:
        builder = BulkSurfaceWorkChain.get_builder()
    fingerprints = []
    for stage, request in record["stage_requests"].items():
        child, _ = backend.build_job(PreviewRequest.model_validate(request), f"{label}:{stage}")
        builder[stage] = child._inputs(prune=True)
        fingerprints.append(backend.execution_fingerprint(child))
    if record.get("reuse_nodes"):
        builder.reuse = {
            key: orm.load_node(uuid=uuid) for key, uuid in record["reuse_nodes"].items()
        }
        fingerprints.append(json.dumps(record["reuse_nodes"], sort_keys=True))
    if record.get("bulk_source_uuid"):
        source = completed_bulk(record["bulk_source_uuid"])
        builder.bulk_structure = source.outputs.structure
        builder.bulk_results = source.outputs.misc
        fingerprints.extend([source.uuid, source.outputs.structure.uuid, source.outputs.misc.uuid])
    builder.plan = orm.Dict(dict=record["surface_plan"])
    builder.metadata.label = label
    fingerprints.append(json.dumps(record["surface_plan"], sort_keys=True))
    return builder, hashlib.sha256(json.dumps(fingerprints).encode()).hexdigest()
