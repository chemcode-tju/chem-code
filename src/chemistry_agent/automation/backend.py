import hashlib
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
from aiida import orm
from aiida.common.exceptions import NotExistent
from aiida.engine import run_get_node, submit
from aiida.engine.daemon.client import get_daemon_client
from aiida.engine.processes.builder import ProcessBuilder
from aiida.plugins import CalculationFactory
from ase import Atoms
from ase.io import read
from filelock import FileLock

from chemistry_agent.automation.configuration import (
    CalculationConfiguration,
    CalculationProtocol,
    PotentialSource,
)
from chemistry_agent.automation.constraints import (
    positions_dof,
    restore_constraints,
    structure_text,
)
from chemistry_agent.automation.models import (
    CodeCreate,
    PreviewRequest,
    Resources,
    ServerCreate,
    StructureInput,
)
from chemistry_agent.errors import AgentError

MARKER = "Chemistry Agent automation v1"
PREFIX = "chem-agent:"


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_structure(specification: StructureInput) -> Atoms:
    """Reject lossy imports, multiple frames, invalid cells and non-finite positions."""
    try:
        frames = read(io.StringIO(specification.content), format=specification.format, index=":")
        if len(frames) != 1:
            raise ValueError("每个任务必须提供且只提供一个结构。")
        atoms = frames[0]
        restore_constraints(atoms)
        if not 1 <= len(atoms) <= 10000:
            raise ValueError("结构原子数必须为 1–10000。")
        if not np.isfinite(atoms.positions).all() or not np.isfinite(atoms.cell.array).all():
            raise ValueError("结构包含非有限坐标。")
        if atoms.cell.rank != 3 or abs(atoms.cell.volume) < 1e-6 or not atoms.pbc.all():
            raise ValueError("首版只接受具有完整晶胞的三维周期结构。")
        positions_dof(atoms)
        if np.any(atoms.get_initial_magnetic_moments()) or np.any(atoms.get_initial_charges()):
            raise ValueError("结构含初始磁矩或电荷，请显式转为 INCAR 配置后再导入。")
        return atoms
    except (ValueError, TypeError, IndexError, KeyError, RuntimeError) as exc:
        raise AgentError("invalid_structure", str(exc), 422) from exc


def server_record(computer: orm.Computer) -> dict:
    return {
        "id": computer.uuid,
        "label": computer.label,
        "hostname": computer.hostname,
        "scheduler": computer.scheduler_type,
        "workdir": computer.get_workdir(),
        "mpi_command": computer.get_mpirun_command(),
    }


def load_server(server_id: str) -> orm.Computer:
    computer = orm.load_computer(uuid=str(UUID(server_id)))
    if computer.description != MARKER:
        raise AgentError("server_not_owned", "此服务器不是本模块创建的配置。", 404)
    return computer


def status() -> dict:
    servers = [
        server_record(item)
        for item in orm.QueryBuilder()
        .append(orm.Computer, filters={"description": MARKER})
        .all(flat=True)
    ]
    codes = []
    for code in orm.QueryBuilder().append(orm.InstalledCode).all(flat=True):
        if code.computer.description == MARKER:
            codes.append(
                {
                    "id": code.uuid,
                    "label": code.label,
                    "server_id": code.computer.uuid,
                    "plugin": code.default_calc_job_plugin,
                    "executable": str(code.filepath_executable),
                    "environment": code.prepend_text,
                }
            )
    return {
        "configured": True,
        "daemon_running": get_daemon_client().is_daemon_running,
        "servers": servers,
        "codes": codes,
        "checked_at": now(),
    }


def create_server(payload: dict) -> dict:
    body = ServerCreate.model_validate(payload)
    label = f"chem-{body.label}"
    if orm.QueryBuilder().append(orm.Computer, filters={"label": label}).count():
        raise AgentError("server_exists", "名称已存在，请选择已有服务器或使用新名称。", 409)
    resolved = subprocess.run(
        ["ssh", "-G", body.hostname], capture_output=True, text=True, timeout=10, check=True
    )
    options: dict[str, list[str]] = {}
    for line in resolved.stdout.splitlines():
        key, _, value = line.partition(" ")
        options.setdefault(key, []).append(value)
    computer = orm.Computer(
        label=label,
        hostname=options["hostname"][0],
        description=MARKER,
        transport_type="core.ssh_async",
        scheduler_type=f"core.{body.scheduler}",
        workdir=body.workdir,
    )
    computer.set_minimum_job_poll_interval(15)
    computer.set_mpirun_command(["mpirun", "-np", "{tot_num_mpiprocs}"])
    computer.store()
    computer.configure(
        host=body.hostname,
        backend="asyncssh",
        max_io_allowed=4,
        safe_interval=5.0,
        use_login_shell=True,
    )
    orm.InstalledCode(
        label=f"structure-probe-{computer.uuid[:8]}",
        computer=computer,
        filepath_executable="/bin/sh",
        default_calc_job_plugin="chemistry.structure_probe",
    ).store()
    return server_record(computer)


def test_server(payload: dict) -> dict:
    computer = load_server(payload["server_id"])
    checks = []
    # No mkdir, upload or scheduler submission: this endpoint is read-only.
    with computer.get_transport() as transport:
        code, output, error = transport.exec_command_wait("hostname")
        if code:
            raise AgentError("ssh_probe_failed", "连接已建立，但无法执行 hostname。", 502)
        checks.append({"check": "ssh", "ok": True, "detail": output.strip()})
        scheduler = computer.get_scheduler()
        scheduler.set_transport(transport)
        scheduler.get_jobs(user=transport.whoami())
        checks.append({"check": "scheduler", "ok": True, "detail": computer.scheduler_type})
        path = computer.get_workdir()
        while not transport.isdir(path) and path != "/":
            path = str(Path(path).parent)
        code, _, _ = transport.exec_command_wait(f"test -w {shlex.quote(path)}")
        checks.append({"check": "workdir_parent", "ok": code == 0, "detail": path})
    return {"server_id": computer.uuid, "checks": checks, "checked_at": now()}


def validate_gpu_type(computer: orm.Computer, resources: Resources) -> None:
    """Check configured Slurm GRES names, irrespective of current node occupancy."""
    if not resources.gpus_per_node or not resources.gpu_type:
        return
    if computer.scheduler_type != "core.slurm":
        raise AgentError(
            "gpu_scheduler_unsupported", "PBS GPU 资源语法需按站点配置，首版暂不支持。", 422
        )
    with computer.get_transport() as transport:
        code, output, _ = transport.exec_command_wait(
            f"sinfo --noheader --partition={shlex.quote(resources.queue)} --format=%G"
        )
    if code:
        raise AgentError("gpu_query_failed", "无法查询分区 GPU 类型，请检查队列配置和连接。", 422)
    available = sorted(set(re.findall(r"(?:^|[,\s])gpu:([^,:()\s]+):\d+", output)))
    if resources.gpu_type not in available:
        names = ", ".join(available) or "未发现具名 GPU"
        raise AgentError(
            "gpu_type_unavailable",
            f"队列 {resources.queue} 未配置 GPU 类型 {resources.gpu_type}；"
            f"可用类型：{names}。类型名区分大小写，请使用调度器原名。",
            422,
        )


def build_job(body: PreviewRequest, label: str) -> tuple[ProcessBuilder, Atoms]:
    computer = load_server(str(body.server_id))
    validate_gpu_type(computer, body.resources)
    atoms = read_structure(body.structure)
    # ASE surface tags identify layers, not different chemical species/kinds.
    chemical_atoms = atoms.copy()
    chemical_atoms.set_tags(0)
    structure = orm.StructureData(ase=chemical_atoms)
    if body.kind == "structure_probe":
        if (
            body.resources.nodes != 1
            or body.resources.ranks_per_node != 1
            or body.resources.wallclock_seconds > 120
            or body.resources.gpus_per_node
        ):
            raise AgentError(
                "probe_resource_limit", "结构联通测试限单节点、单进程和 120 秒以内。", 422
            )
        builder = CalculationFactory("chemistry.structure_probe").get_builder()
        builder.code = orm.load_code(f"structure-probe-{computer.uuid[:8]}@{computer.label}")
    else:
        if not body.code_id or not body.potential_family or not body.incar:
            raise AgentError(
                "vasp_not_configured", "VASP 需要 Code、赝势族、映射和显式 INCAR 参数。", 422
            )
        code = orm.load_code(uuid=str(body.code_id))
        if code.computer.uuid != computer.uuid or code.default_calc_job_plugin != "vasp.vasp":
            raise AgentError("code_mismatch", "VASP Code 与服务器或计算插件不匹配。", 422)
        from aiida_vasp.data.potcar import PotcarData

        builder = CalculationFactory("vasp.vasp").get_builder()
        builder.code = code
        builder.parameters = orm.Dict(
            dict={key.lower(): value for key, value in body.incar.items()}
        )
        builder.settings = orm.Dict(dict={"ADDITIONAL_RETRIEVE_LIST": ["OSZICAR", "XDATCAR"]})
        builder.kpoints = orm.KpointsData()
        builder.kpoints.set_kpoints_mesh(body.kmesh)
        if atoms.constraints:
            builder.dynamics = orm.Dict(dict={"positions_dof": positions_dof(atoms)})
        builder.potential = PotcarData.get_potcars_from_structure(
            structure, body.potential_family, mapping=body.potential_mapping
        )
    builder.structure = structure
    resources = body.resources
    builder.metadata.label = label
    builder.metadata.options.resources = {
        "num_machines": resources.nodes,
        "num_mpiprocs_per_machine": resources.ranks_per_node,
    }
    builder.metadata.options.queue_name = resources.queue
    builder.metadata.options.max_wallclock_seconds = resources.wallclock_seconds
    builder.metadata.options.max_memory_kb = resources.memory_mb * 1024
    builder.metadata.options.withmpi = body.kind == "vasp"
    if resources.gpus_per_node:
        if computer.scheduler_type != "core.slurm":
            raise AgentError(
                "gpu_scheduler_unsupported", "PBS GPU 资源语法需按站点配置，首版暂不支持。", 422
            )
        gpu = f"gpu:{resources.gpus_per_node}"
        if resources.gpu_type:
            gpu = f"gpu:{resources.gpu_type}:{resources.gpus_per_node}"
        builder.metadata.options.custom_scheduler_commands = f"#SBATCH --gres={gpu}"
    if resources.account:
        builder.metadata.options.account = resources.account
    return builder, atoms


def automation_directory() -> Path:
    path = Path(os.environ["AIIDA_PATH"]) / "chemistry-automation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview(payload: dict) -> dict:
    body = PreviewRequest.model_validate(payload)
    if body.workflow == "bulk_surface":
        from chemistry_agent.automation.surface import preview_surface

        return preview_surface(body)
    if body.workflow == "relax_static":
        return preview_workflow(body)
    preview_id = str(uuid4())
    builder, atoms = build_job(body, PREFIX + preview_id)
    directory = automation_directory() / preview_id
    directory.mkdir(mode=0o700)
    builder.metadata.dry_run = True
    builder.metadata.store_provenance = False
    previous_cwd = Path.cwd()
    try:
        os.chdir(directory)
        _, node = run_get_node(builder)
    finally:
        os.chdir(previous_cwd)
    folder = Path(node.dry_run_info["folder"])
    files = {}
    for name in ("_aiidasubmit.sh", "POSCAR", "INCAR", "KPOINTS", "probe.sh"):
        if (folder / name).is_file():
            files[name] = (folder / name).read_text()
    record = {
        "preview_id": preview_id,
        "created_at": now(),
        "request": body.model_dump(mode="json"),
        "files": files,
        "structure": {
            "formula": atoms.get_chemical_formula(),
            "atoms": len(atoms),
            "volume_angstrom3": atoms.cell.volume,
        },
        "kind": body.kind,
        "resources": body.resources.model_dump(),
        "code_uuid": builder.code.uuid,
        "execution_fingerprint": execution_fingerprint(builder),
        "server": server_record(builder.code.computer),
    }
    # Hash public generated inputs; POTCAR is intentionally neither returned nor published.
    record["input_sha256"] = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    (directory / "preview.json").write_text(json.dumps(record, ensure_ascii=False))
    return {key: value for key, value in record.items() if key != "request"}


def workflow_builder(record: dict, label: str) -> tuple[ProcessBuilder, str]:
    from chemistry_agent.automation.workflows import RelaxStaticWorkChain

    builder = RelaxStaticWorkChain.get_builder()
    fingerprints = []
    for stage in ("relax", "singlepoint"):
        child, _ = build_job(
            PreviewRequest.model_validate(record["stage_requests"][stage]), f"{label}:{stage}"
        )
        fingerprints.append(execution_fingerprint(child))
        builder[stage] = child._inputs(prune=True)
    builder.metadata.label = label
    fingerprint = hashlib.sha256(json.dumps(fingerprints).encode()).hexdigest()
    return builder, fingerprint


def preview_workflow(body: PreviewRequest) -> dict:
    """Preview both stages without executing them; static structure is late-bound."""
    incar = {key.upper(): value for key, value in body.incar.items()}
    if (
        body.kind != "vasp"
        or incar.get("NSW", 0) < 1
        or incar.get("IBRION", -1) < 1
        or incar.get("ISIF") != 2
        or incar.get("EDIFFG", 0) >= 0
    ):
        raise AgentError(
            "workflow_requires_relax",
            "请选择固定晶胞优化方案，设置 NSW>0、IBRION>0、ISIF=2、EDIFFG<0。",
            422,
        )
    requests, stages, files, fingerprints = {}, [], {}, []
    for stage in ("relax", "singlepoint"):
        child = body.model_dump(mode="json")
        child["workflow"] = "single"
        child["incar"] = dict(incar)
        if stage == "singlepoint":
            child["incar"].update(NSW=0, IBRION=-1)
        result = preview(child)
        fingerprints.append(result["execution_fingerprint"])
        requests[stage] = child
        stages.append(
            {
                "stage": stage,
                "incar": child["incar"],
                "resources": child["resources"],
                "structure_source": "uploaded" if stage == "relax" else "relax.outputs.structure",
            }
        )
        files.update(
            {
                f"{stage}/{name}": content
                for name, content in result["files"].items()
                if stage == "relax" or name != "POSCAR"
            }
        )
    identifier = str(uuid4())
    record = {
        **result,
        "preview_id": identifier,
        "created_at": now(),
        "workflow": "relax_static",
        "request": body.model_dump(mode="json"),
        "stage_requests": requests,
        "stages": stages,
        "files": files,
        "max_calculations": 2,
        "total_wallclock_limit_seconds": 2 * body.resources.wallclock_seconds,
        "workflow_note": (
            "优化通过电子与结构收敛检查后，自动用优化结构计算单点；第二步 POSCAR 在运行时生成。"
        ),
    }
    record["execution_fingerprint"] = hashlib.sha256(json.dumps(fingerprints).encode()).hexdigest()
    record["input_sha256"] = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    directory = automation_directory() / identifier
    directory.mkdir(mode=0o700)
    (directory / "preview.json").write_text(json.dumps(record, ensure_ascii=False))
    return {key: value for key, value in record.items() if key not in {"request", "stage_requests"}}


def job_record(node: orm.ProcessNode) -> dict:
    is_calculation = isinstance(node, orm.CalcJobNode)
    result = {
        "id": node.uuid,
        "pk": node.pk,
        "label": node.label,
        "process_state": node.process_state.value if node.process_state else "unknown",
        "process_status": node.process_status,
        "paused": node.paused,
        "scheduler_state": node.get_scheduler_state().value
        if is_calculation and node.get_scheduler_state()
        else None,
        "scheduler_job_id": node.get_job_id() if is_calculation else None,
        "scheduler_checked_at": node.get_scheduler_lastchecktime().isoformat()
        if is_calculation and node.get_scheduler_lastchecktime()
        else None,
        "exit_status": node.exit_status,
        "exit_message": node.exit_message,
        "finished_ok": node.is_finished_ok,
        "retrieved": "retrieved" in node.outputs,
        "created_at": node.ctime.isoformat(),
        "updated_at": node.mtime.isoformat(),
        "checked_at": now(),
        "scientific_validation": "not_assessed",
    }
    if "report" in node.outputs:
        result["report"] = node.outputs.report.get_dict()
        result["scientific_validation"] = "not_applicable_probe"
    if "misc" in node.outputs:
        result["results"] = node.outputs.misc.get_dict()
    if "structure" in node.outputs:
        final = node.outputs.structure.get_ase()
        result["output_structure"] = {
            "uuid": node.outputs.structure.uuid,
            "atoms": len(final),
            "cell_angstrom": final.cell.tolist(),
        }
    if is_calculation:
        result.update(submission_diagnostics(node))
    else:
        result["workflow"] = (
            "bulk_surface"
            if "BulkSurfaceWorkChain" in (node.process_type or "")
            or node.process_type == "aiida.workflows:chemistry.bulk_surface"
            else "relax_static"
        )
        children = [child for child in node.called if isinstance(child, orm.CalcJobNode)]
        result["steps"] = [job_record(child) for child in children]
        if result["workflow"] == "co_adsorption" and node.exit_status == 434:
            from chemistry_agent.automation.adsorption import valid_branch

            for step, child in zip(result["steps"], children, strict=True):
                if step["label"].split(":")[-1] in {"gas", "top", "bridge", "hollow"}:
                    step["branch_valid"] = bool(valid_branch(child))
        if "adsorption" in node.outputs:
            result["adsorption"] = node.outputs.adsorption.get_dict()
        if "co_adsorption" in (node.process_type or "") or "CoAdsorption" in (
            node.process_type or ""
        ):
            result["workflow"] = "co_adsorption"
        if "summary" in node.outputs:
            result["summary"] = node.outputs.summary.get_dict()
        if "results" in node.outputs:
            result["results"] = node.outputs.results.get_dict()
    return result


def submission_diagnostics(node: orm.CalcJobNode) -> dict:
    """Surface pre-scheduler failures that have no retrieved stdout/stderr yet."""
    errors = []
    logs = orm.Log.collection.find(
        {"dbnode_id": node.pk, "levelname": {"in": ["ERROR", "WARNING"]}},
        order_by={"time": "desc"},
        limit=3,
    )
    for log in logs:
        lines = [line.strip() for line in log.message.splitlines() if line.strip()]
        useful = [line for line in lines if "sbatch:" in line or "SchedulerError:" in line]
        errors.append(
            {
                "time": log.time.isoformat(),
                "level": log.levelname,
                "message": "\n".join(useful or lines[-2:])[-1500:],
            }
        )
    # Show the actionable scheduler response before a later generic retry-limit warning.
    errors.sort(key=lambda item: "sbatch: error:" not in item["message"])
    if node.get_job_id():
        state = "accepted_by_scheduler"
    elif any("sbatch: error:" in item["message"] for item in errors):
        state = "rejected_by_scheduler"
    elif errors:
        state = "submission_error" if not node.is_terminated else "not_submitted"
    else:
        state = "not_submitted"
    return {"submission_state": state, "recent_process_errors": errors}


def submit_preview(payload: dict) -> dict:
    preview_id = str(UUID(payload["preview_id"]))
    root = automation_directory()
    path = root / preview_id / "preview.json"
    if not path.is_file():
        raise AgentError("preview_not_found", "找不到预览，请先生成任务预览。", 404)
    record = json.loads(path.read_text())
    label = PREFIX + preview_id
    # A durable reservation precedes submit. Ambiguous crashes never trigger blind re-submission.
    with FileLock(str(root / "submission.lock"), timeout=10):
        existing = (
            orm.QueryBuilder().append(orm.ProcessNode, filters={"label": label}).all(flat=True)
        )
        if existing:
            return {"reused": True, "job": job_record(existing[0])}
        with sqlite3.connect(root / "submissions.sqlite3") as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, created TEXT)"
            )
            if database.execute("SELECT id FROM reservations WHERE id=?", (preview_id,)).fetchone():
                raise AgentError(
                    "submission_uncertain",
                    "上次提交结果不明，已阻止重复提交；请检查 AiiDA 记录。",
                    409,
                )
            if not get_daemon_client().is_daemon_running:
                raise AgentError(
                    "daemon_not_running", "AiiDA daemon 未运行，请先启动后台服务。", 503
                )
            if record.get("workflow") == "bulk_surface":
                from chemistry_agent.automation.surface import surface_builder

                builder, fingerprint = surface_builder(record, label)
            elif record.get("workflow") == "relax_static":
                builder, fingerprint = workflow_builder(record, label)
            else:
                builder, _ = build_job(PreviewRequest.model_validate(record["request"]), label)
                fingerprint = execution_fingerprint(builder)
            if fingerprint != record["execution_fingerprint"]:
                raise AgentError("preview_stale", "服务器或 Code 已变化，请重新生成预览。", 409)
            database.execute("INSERT INTO reservations VALUES (?,?)", (preview_id, now()))
            database.commit()
            node = submit(builder)
        return {"reused": False, "job": job_record(node)}


def jobs() -> dict:
    query = orm.QueryBuilder().append(
        orm.ProcessNode, filters={"label": {"like": PREFIX + "%"}}, tag="job"
    )
    query.order_by({"job": {"ctime": "desc"}}).limit(100)
    return {"jobs": [job_record(node) for node in query.all(flat=True)], "checked_at": now()}


def tracked_jobs(payload: dict) -> dict:
    identifiers = [str(UUID(item)) for item in payload.get("job_ids", [])[:200]]
    nodes = (
        orm.QueryBuilder()
        .append(orm.ProcessNode, filters={"uuid": {"in": identifiers}})
        .all(flat=True)
        if identifiers
        else []
    )
    return {
        "jobs": [job_record(node) for node in nodes if node.label.startswith(PREFIX)],
        "checked_at": now(),
    }


def validate_structure(payload: dict) -> dict:
    specification = StructureInput.model_validate(payload)
    atoms = read_structure(specification)
    return {
        "structure": specification.model_dump(),
        "extxyz": structure_text(atoms),
        "positions_dof": positions_dof(atoms),
        "formula": atoms.get_chemical_formula(),
        "atoms": len(atoms),
        "elements": sorted(set(atoms.get_chemical_symbols())),
        "cell_angstrom": atoms.cell.tolist(),
        "volume_angstrom3": atoms.get_volume(),
        "input_sha256": hashlib.sha256(specification.content.encode()).hexdigest(),
    }


def catalog() -> dict:
    from aiida_vasp.data.potcar import PotcarData

    result = status()
    families = []
    for group in PotcarData.get_potcar_groups():
        potentials = [
            {"element": node.element, "full_name": node.full_name}
            for node in group.nodes
            if isinstance(node, PotcarData)
        ]
        families.append({"name": group.label, "potentials": potentials})
    result["potential_families"] = families
    configuration = CalculationConfiguration(Path(os.environ["AIIDA_PATH"])).read()
    result["protocols"] = configuration["protocols"]
    result["potential_sources"] = [
        {key: value for key, value in source.items() if key != "root"}
        for source in configuration["sources"]
    ]
    return result


def save_configuration(payload: dict) -> dict:
    configuration = CalculationConfiguration(Path(os.environ["AIIDA_PATH"]))
    kind = payload["kind"]
    if kind == "sources":
        source = PotentialSource.model_validate(payload["value"])
        for existing in configuration.read()["sources"]:
            if existing["family"] == source.family and (
                existing["root"] != source.root or existing["server_id"] != str(source.server_id)
            ):
                raise AgentError(
                    "potential_source_conflict",
                    "同一赝势族不能切换到另一套来源，请使用新的族名。",
                    422,
                )
        computer = load_server(str(source.server_id))
        with computer.get_transport() as transport:
            if not transport.isdir(source.root):
                raise AgentError("potential_source_missing", "服务器赝势目录不存在。", 422)
        return configuration.save(kind, source)
    protocol = CalculationProtocol.model_validate(payload["value"])
    computer = load_server(str(protocol.server_id))
    code = orm.load_code(uuid=str(protocol.code_id))
    if code.computer.uuid != computer.uuid or code.default_calc_job_plugin != "vasp.vasp":
        raise AgentError("code_mismatch", "计算方案的 Code 与服务器不匹配。", 422)
    validate_gpu_type(computer, protocol.resources)
    configuration.get("sources", protocol.potential_source_id)
    return configuration.save("protocols", protocol)


def resolve_calculation(payload: dict) -> dict:
    """Resolve a saved recipe; copy only missing approved potentials, never expose contents."""
    from aiida_vasp.data.potcar import PotcarData, PotcarGroup

    configuration = CalculationConfiguration(Path(os.environ["AIIDA_PATH"]))
    protocol_id = payload.get("protocol_id")
    if not protocol_id:
        defaults = [item for item in configuration.read()["protocols"] if item["default"]]
        if len(defaults) != 1:
            raise AgentError(
                "protocol_required", "请从 calculation_setup 中选择一个已注册计算方案。", 422
            )
        protocol_id = defaults[0]["id"]
    protocol = CalculationProtocol.model_validate(configuration.get("protocols", protocol_id))
    source = PotentialSource.model_validate(
        configuration.get("sources", protocol.potential_source_id)
    )
    atoms = read_structure(StructureInput.model_validate(payload["structure"]))
    elements = set(atoms.get_chemical_symbols())
    plan = payload.get("surface_plan") or {}
    atom_limit = len(atoms)
    if plan.get("co_sites"):
        elements.update(("C", "O"))
        atom_limit = max(atom_limit, int(np.prod(plan["size"])) * plan["layers"] + 2)
    elements = sorted(elements)
    if atom_limit > protocol.max_atoms or (
        protocol.allowed_elements and not set(elements) <= set(protocol.allowed_elements)
    ):
        raise AgentError(
            "protocol_scope_mismatch", "结构超出方案的元素或原子数范围，请选择适用方案。", 422
        )
    mapping = {element: protocol.potential_mapping.get(element, element) for element in elements}
    incar = {key.upper(): value for key, value in protocol.incar.items()}
    incar.update({key.upper(): value for key, value in payload.get("incar", {}).items()})
    if str(incar.get("GGA", "PE")).upper() != "PE" or incar.get("METAGGA"):
        raise AgentError("functional_mismatch", "快捷方案目前仅支持明确的 PBE 设置。", 422)
    group, _ = PotcarGroup.collection.get_or_create(label=source.family)
    registered = []
    missing = []
    for element, name in mapping.items():
        candidates = [
            node for node in group.nodes if isinstance(node, PotcarData) and node.full_name == name
        ]
        if len(candidates) > 1 or (candidates and candidates[0].element != element):
            raise AgentError(
                "potential_ambiguous", f"赝势 {name} 有重复版本或元素不匹配，请修正赝势族。", 422
            )
        if candidates:
            functional = candidates[0].base.attributes.get(
                "functional", candidates[0].base.attributes.get("unctional", "")
            )
            if "PBE" not in str(functional).upper():
                raise AgentError("potential_functional_mismatch", f"赝势 {name} 不是 PBE。", 422)
            registered.append(
                {
                    "element": element,
                    "full_name": name,
                    "uuid": candidates[0].uuid,
                    "sha512": candidates[0].sha512,
                    "imported": False,
                }
            )
        else:
            missing.append((element, name))
    if missing:
        computer = load_server(str(source.server_id))
        with tempfile.TemporaryDirectory(prefix="chem-potcar-") as temporary:
            with computer.get_transport() as transport:
                # Check all required files first; paths come only from user configuration.
                for _, name in missing:
                    path = f"{source.root}/{name}/POTCAR"
                    if not transport.isfile(path):
                        raise AgentError(
                            "potential_missing", f"已注册赝势源缺少 {name}/POTCAR。", 422
                        )
                for element, name in missing:
                    local = Path(temporary) / source.family / name / "POTCAR"
                    local.parent.mkdir(parents=True, exist_ok=True)
                    transport.getfile(f"{source.root}/{name}/POTCAR", str(local))
                    node, _ = PotcarData.get_or_create_from_file(local)
                    if node.element != element or node.full_name != name:
                        raise AgentError(
                            "potential_mismatch", f"赝势 {name} 的实际元素/名称不匹配。", 422
                        )
                    functional = node.base.attributes.get(
                        "functional", node.base.attributes.get("unctional", "")
                    )
                    if "PBE" not in str(functional).upper():
                        raise AgentError(
                            "potential_functional_mismatch", f"赝势 {name} 不是 PBE。", 422
                        )
                    group.add_nodes(node)
                    registered.append(
                        {
                            "element": element,
                            "full_name": name,
                            "uuid": node.uuid,
                            "sha512": node.sha512,
                            "imported": True,
                        }
                    )
    mesh = payload.get("kmesh") or protocol.kmesh
    if not payload.get("kmesh") and protocol.kpoint_spacing:
        mesh = tuple(
            int(max(1, np.ceil(length / protocol.kpoint_spacing)))
            for length in 2 * np.pi * np.linalg.norm(atoms.cell.reciprocal(), axis=1)
        )
    request = PreviewRequest(
        kind="vasp",
        server_id=protocol.server_id,
        code_id=protocol.code_id,
        structure=StructureInput.model_validate(payload["structure"]),
        resources=protocol.resources,
        incar=incar,
        kmesh=mesh,
        potential_family=source.family,
        potential_mapping=mapping,
    )
    return {
        "request": request.model_dump(mode="json"),
        "protocol": protocol.model_dump(mode="json"),
        "potentials": registered,
        "scientific_validation": "not_assessed",
    }


def load_job(identifier: str) -> orm.ProcessNode:
    """Resolve managed jobs once for read and structure operations."""
    try:
        node = orm.load_node(int(identifier) if identifier.isdigit() else str(UUID(identifier)))
    except (ValueError, NotExistent) as exc:
        raise AgentError("job_not_found", "没有找到该 AiiDA 任务。", 404) from exc
    if not isinstance(node, (orm.CalcJobNode, orm.WorkChainNode)) or not node.label.startswith(
        PREFIX
    ):
        raise AgentError("job_not_owned", "只能查询本模块创建的计算任务。", 404)
    return node


def session_job(payload: dict) -> dict:
    """Follow actual CALL links, so nested workflow children inherit session access."""
    node = load_job(payload["job_id"])
    roots = set(payload["root_ids"])
    ancestor = node
    while ancestor is not None:
        if ancestor.uuid in roots:
            return {"id": node.uuid, "pk": node.pk}
        ancestor = ancestor.caller
    raise AgentError("job_not_in_session", "任务不属于当前对话的计算流程。", 403)


def job_details(payload: dict) -> dict:
    node = load_job(payload["job_id"])
    result = job_record(node)
    result["retrieved_files"] = []
    result["diagnostics"] = {}
    if isinstance(node, orm.WorkChainNode):
        result["steps"] = [
            job_details({"job_id": child.uuid})
            for child in node.called
            if isinstance(child, orm.CalcJobNode) and child.label.startswith(PREFIX)
        ]
    if "retrieved" in node.outputs:
        repository = node.outputs.retrieved.base.repository
        allowed = {
            "OUTCAR",
            "vasprun.xml",
            "CONTCAR",
            "vasp_output",
            "_scheduler-stdout.txt",
            "_scheduler-stderr.txt",
        }
        names = set(repository.list_object_names()) & allowed
        result["retrieved_files"] = sorted(names)
        for name in ("vasp_output", "_scheduler-stderr.txt"):
            if name in names:
                with repository.open(name, mode="rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 8000))
                    result["diagnostics"][name] = stream.read(8000).decode(errors="replace")
    return result


def execution_fingerprint(builder: ProcessBuilder) -> str:
    """Detect edits to code, resources and pseudopotential selection after preview."""
    computer = builder.code.computer
    state = {
        "server": server_record(computer),
        "mpi": computer.get_mpirun_command(),
        "transport": computer.transport_type,
        "computer_prepend": computer.get_prepend_text(),
        "computer_append": computer.get_append_text(),
        "code": builder.code.base.attributes.all,
        "code_uuid": builder.code.uuid,
        "potentials": {key: value.uuid for key, value in builder.get("potential", {}).items()},
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def create_code(payload: dict) -> dict:
    body = CodeCreate.model_validate(payload)
    computer = load_server(str(body.server_id))
    label = f"chem-{body.label}"
    if orm.QueryBuilder().append(orm.InstalledCode, filters={"label": label}).count():
        raise AgentError("code_exists", "Code 名称已存在，请选择已有 Code。", 409)
    with computer.get_transport() as transport:
        if not transport.isfile(body.executable):
            raise AgentError("executable_missing", "远端可执行文件不存在。", 422)
        if body.environment_script and not transport.isfile(body.environment_script):
            raise AgentError("environment_missing", "远端环境脚本不存在。", 422)
    prepend = f"source {shlex.quote(body.environment_script)}" if body.environment_script else ""
    code = orm.InstalledCode(
        label=label,
        computer=computer,
        filepath_executable=body.executable,
        default_calc_job_plugin="vasp.vasp",
        prepend_text=prepend,
    ).store()
    return {"id": code.uuid, "label": code.label, "server_id": computer.uuid}
