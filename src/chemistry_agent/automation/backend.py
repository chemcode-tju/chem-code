import hashlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
from aiida import orm
from aiida.engine import run_get_node, submit
from aiida.engine.daemon.client import get_daemon_client
from aiida.engine.processes.builder import ProcessBuilder
from aiida.plugins import CalculationFactory
from ase import Atoms
from ase.io import read
from filelock import FileLock

from chemistry_agent.automation.models import (
    CodeCreate,
    PreviewRequest,
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
        if not 1 <= len(atoms) <= 10000:
            raise ValueError("结构原子数必须为 1–10000。")
        if not np.isfinite(atoms.positions).all() or not np.isfinite(atoms.cell.array).all():
            raise ValueError("结构包含非有限坐标。")
        if atoms.cell.rank != 3 or abs(atoms.cell.volume) < 1e-6 or not atoms.pbc.all():
            raise ValueError("首版只接受具有完整晶胞的三维周期结构。")
        if atoms.constraints:
            raise ValueError("首版暂不转换原子约束；不能静默丢弃 Selective Dynamics。")
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


def build_job(body: PreviewRequest, label: str) -> tuple[ProcessBuilder, Atoms]:
    computer = load_server(str(body.server_id))
    atoms = read_structure(body.structure)
    structure = orm.StructureData(ase=atoms)
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
        builder.kpoints = orm.KpointsData()
        builder.kpoints.set_kpoints_mesh(body.kmesh)
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


def job_record(node: orm.CalcJobNode) -> dict:
    result = {
        "id": node.uuid,
        "pk": node.pk,
        "label": node.label,
        "process_state": node.process_state.value if node.process_state else "unknown",
        "process_status": node.process_status,
        "paused": node.paused,
        "scheduler_state": node.get_scheduler_state().value if node.get_scheduler_state() else None,
        "scheduler_job_id": node.get_job_id(),
        "exit_status": node.exit_status,
        "exit_message": node.exit_message,
        "finished_ok": node.is_finished_ok,
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
    return result


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
            orm.QueryBuilder().append(orm.CalcJobNode, filters={"label": label}).all(flat=True)
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
            builder, _ = build_job(PreviewRequest.model_validate(record["request"]), label)
            if execution_fingerprint(builder) != record["execution_fingerprint"]:
                raise AgentError("preview_stale", "服务器或 Code 已变化，请重新生成预览。", 409)
            database.execute("INSERT INTO reservations VALUES (?,?)", (preview_id, now()))
            database.commit()
            node = submit(builder)
        return {"reused": False, "job": job_record(node)}


def jobs() -> dict:
    query = orm.QueryBuilder().append(
        orm.CalcJobNode, filters={"label": {"like": PREFIX + "%"}}, tag="job"
    )
    query.order_by({"job": {"ctime": "desc"}}).limit(100)
    return {"jobs": [job_record(node) for node in query.all(flat=True)], "checked_at": now()}


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
