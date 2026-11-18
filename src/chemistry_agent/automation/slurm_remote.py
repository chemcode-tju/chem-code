"""Dependency-free helper sent over SSH; it is not installed on the cluster.

Exactly one JSON request/response per connection. A durable reservation and Slurm
job name reconcile an interrupted sbatch without issuing another submission.
"""

import base64
import fcntl
import json
import math
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

from chemistry_agent.automation.result_reader import result_snapshot

VASP_OUTCAR_LIMIT = 32 * 1024 * 1024

TERMINAL_STATES = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "REVOKED",
}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def run(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, capture_output=True, text=True, timeout=25, check=False)


def safe_file(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "archive" in path.parts:
        raise ValueError("文件须位于任务目录内。")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("文件链接越过任务目录。")
    return target


def recover(root: Path, receipt: dict) -> dict:
    name = receipt["name"]
    ids = set()
    listing = run(["squeue", "-h", "-u", receipt["user"], "-n", name, "-o", "%i|%j"])
    for line in listing.stdout.splitlines():
        parts = line.strip().split("|")
        if (
            len(parts) == 2
            and parts[1] == name
            and parts[0].split("_", 1)[0].isdigit()
            and "." not in parts[0]
        ):
            ids.add(parts[0].split("_", 1)[0])
    accounting = run(
        [
            "sacct",
            "-n",
            "-P",
            "-S",
            receipt["since"],
            "--name",
            name,
            "-u",
            receipt["user"],
            "--format=JobID%100,JobName%80",
        ]
    )
    for line in accounting.stdout.splitlines():
        parts = line.strip().split("|")
        if (
            len(parts) >= 2
            and parts[1] == name
            and parts[0].split("_", 1)[0].isdigit()
            and "." not in parts[0]
        ):
            ids.add(parts[0].split("_", 1)[0])
    if len(ids) == 1:
        receipt.update(status="submitted", scheduler_job_id=ids.pop())
        write_json(root / "_chem_receipt.json", receipt)
    return receipt


def submit(root: Path, request: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "_chem_submit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "_chem_receipt.json"
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt["digest"] != request["digest"]:
                raise ValueError("同一任务编号的输入已变化，不能重复提交。")
            if receipt.get("scheduler_job_id"):
                return receipt
            if receipt["status"] == "uncertain":
                return recover(root, receipt)
            # Quota retry requires a persisted completion-event release token.
            release = request.get("release")
            if not release or release in receipt.get("releases", []):
                return receipt
            if receipt["status"] != "quota_wait":
                return receipt
            receipt.setdefault("releases", []).append(release)
        else:
            from datetime import datetime, timedelta

            receipt = {
                "digest": request["digest"],
                "name": "chem-" + request["id"],
                "user": pwd.getpwuid(os.getuid()).pw_name,
                "since": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                "releases": [],
            }
        receipt["status"] = "uncertain"
        write_json(path, receipt)
        result = run(["sbatch", "--parsable", "--chdir", str(root), str(root / "_chem_submit.sh")])
        job_id = result.stdout.strip().split(";")[0]
        if result.returncode == 0 and job_id.isdigit():
            receipt.update(status="submitted", scheduler_job_id=job_id)
        elif result.returncode != 0:
            error = (result.stderr or result.stdout)[-8000:]
            quota = any(
                word in error.lower()
                for word in ("qosmaxsubmit", "assocmaxsubmit", "job violates accounting/qos policy")
            )
            receipt.update(status="quota_wait" if quota else "rejected", error=error)
        write_json(path, receipt)
        return receipt


def status(root: Path) -> dict:
    path = root / "_chem_receipt.json"
    if not path.exists():
        return {"status": "not_submitted"}
    receipt = json.loads(path.read_text())
    if receipt["status"] == "uncertain":
        receipt = recover(root, receipt)
    if not receipt.get("scheduler_job_id"):
        return receipt
    job_id = receipt["scheduler_job_id"]
    query = run(["squeue", "-h", "-j", job_id, "-o", "%T"])
    if query.returncode == 0 and query.stdout.strip():
        return {
            **receipt,
            "scheduler_state": query.stdout.strip().splitlines()[0],
            "terminal": False,
        }
    accounting = run(
        ["sacct", "--array", "-X", "-n", "-P", "-j", job_id, "--format=JobID%100,State,ExitCode"]
    )
    tasks = []
    for line in accounting.stdout.splitlines():
        fields = [field.strip() for field in line.strip().split("|")]
        if len(fields) < 3 or "." in fields[0] or not fields[1].strip():
            continue
        if fields[0] != job_id and not fields[0].startswith(job_id + "_"):
            continue
        state = fields[1].split()[0].rstrip("+")
        suffix = fields[0][len(job_id) :]
        marker = root / ("_chem_exit_code" + suffix)
        tasks.append(
            {
                "id": fields[0],
                "state": state,
                "exit_code": fields[2],
                "script_exit_code": marker.read_text().strip() if marker.exists() else None,
            }
        )
    if tasks:
        terminal = all(task["state"] in TERMINAL_STATES for task in tasks)
        failed = next(
            (
                task
                for task in tasks
                if task["state"] != "COMPLETED"
                or task["exit_code"] != "0:0"
                or task["script_exit_code"] != "0"
            ),
            None,
        )
        representative = failed or tasks[0]
        return {
            **receipt,
            "scheduler_state": representative["state"],
            "terminal": terminal,
            "exit_code": representative["exit_code"],
            "script_exit_code": representative["script_exit_code"],
            "finished_ok": terminal and failed is None,
            "tasks": tasks,
        }
    return {
        **receipt,
        "terminal": False,
        "scheduler_state": "UNKNOWN",
        "sync_error": "Slurm 暂无记账结果；继续跟踪，不推断成功或重新提交。",
    }


def external_job(request: dict) -> dict:
    """Observe a specific existing allocation without creating files or submitting jobs."""
    job_id = request["scheduler_job_id"]
    if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", job_id):
        raise ValueError("使用实际作业号或具体数组元素编号，不使用数组范围或 job step。")
    config = run(["scontrol", "show", "config"])
    cluster = re.search(r"(?m)^ClusterName\s*=\s*(\S+)", config.stdout)
    if config.returncode or not cluster:
        return {"known": False, "terminal": False, "sync_error": "无法核实 Slurm 集群身份。"}
    current = run(["scontrol", "-o", "show", "job", job_id])
    records = []
    for line in current.stdout.splitlines() if current.returncode == 0 else []:
        fields = dict(re.findall(r"(?:^|\s)(\w+)=(.*?)(?=\s+\w+=|$)", line.strip()))
        if "ArrayJobId" in fields and "_" not in job_id:
            raise ValueError("数组父任务请按具体元素编号分别登记，不能把部分元素当整个数组。")
        if fields.get("HetJobId"):
            raise ValueError("异构作业尚未支持接管，请分别核对组件。")
        identity = (
            f"{fields['ArrayJobId']}_{fields.get('ArrayTaskId')}"
            if "ArrayJobId" in fields
            else fields.get("JobId")
        )
        if identity != job_id:
            continue
        records.append(
            {
                "cluster": cluster[1],
                "scheduler_job_id": job_id,
                "submitted_at": fields.get("SubmitTime"),
                "user": fields.get("UserId", "").split("(")[0],
                "remote_directory": fields.get("WorkDir"),
                "scheduler_state": fields.get("JobState", "UNKNOWN"),
                "exit_code": fields.get("ExitCode"),
                "queue": fields.get("Partition"),
                "account": fields.get("Account"),
                "name": fields.get("JobName"),
                "source": "scontrol",
                "stdout": fields.get("StdOut"),
                "stderr": fields.get("StdErr"),
            }
        )
    if not records:
        # Controller records may have expired; query a pinned submission window if known.
        expected = request.get("identity", {})
        accounting = run(
            [
                "sacct",
                "-X",
                "-D",
                "--array",
                "-n",
                "-P",
                "-j",
                job_id,
                "-S",
                expected.get("submitted_at") or request.get("submitted_at") or "1970-01-01",
                "--format=JobID%100,Cluster%128,User%128,Submit,WorkDir%4096,State,ExitCode,Partition%128,Account%128,JobName%256",
            ]
        )
        for line in accounting.stdout.splitlines() if accounting.returncode == 0 else []:
            values = line.removesuffix("|").split("|")
            if len(values) != 10 or values[0].strip() != job_id:
                continue
            keys = (
                "scheduler_job_id",
                "cluster",
                "user",
                "submitted_at",
                "remote_directory",
                "scheduler_state",
                "exit_code",
                "queue",
                "account",
                "name",
            )
            row = dict(zip(keys, (v.strip() for v in values), strict=True))
            if request.get("submitted_at") and row["submitted_at"] != request["submitted_at"]:
                continue
            if expected and any(
                row.get(k) != expected.get(k) for k in ("cluster", "submitted_at", "user")
            ):
                continue
            row["source"] = "sacct"
            records.append(row)
    unique = {json.dumps(row, sort_keys=True): row for row in records}
    if len(unique) != 1:
        return {
            "known": False,
            "terminal": False,
            "sync_error": "作业记录缺失或存在多个实例；请提供提交时间，暂不推断结束。",
        }
    row = next(iter(unique.values()))
    if request.get("submitted_at") and row["submitted_at"] != request["submitted_at"]:
        return {"known": False, "terminal": False, "sync_error": "提交时间不匹配。"}
    if not all(
        row.get(k) and row[k] not in {"Unknown", "N/A", "(null)"}
        for k in ("submitted_at", "remote_directory", "user", "queue")
    ):
        return {"known": False, "terminal": False, "sync_error": "作业身份或目录不完整。"}
    if not Path(row["remote_directory"]).is_absolute():
        return {"known": False, "terminal": False, "sync_error": "Slurm WorkDir 不是绝对路径。"}
    row["remote_directory"] = str(Path(row["remote_directory"]).resolve())
    identity = {
        key: row[key]
        for key in ("cluster", "scheduler_job_id", "submitted_at", "user", "remote_directory")
    }
    if identity["cluster"] != cluster[1] or (
        request.get("identity") and request["identity"] != identity
    ):
        return {"known": False, "terminal": False, "sync_error": "作业身份已变化，停止匹配旧作业。"}
    row["scheduler_state"] = row["scheduler_state"].split()[0].rstrip("+")
    terminal = row["scheduler_state"] in TERMINAL_STATES
    return {
        **row,
        "known": True,
        "identity": identity,
        "status": "submitted",
        "terminal": terminal,
        "finished_ok": terminal
        and row["scheduler_state"] == "COMPLETED"
        and row.get("exit_code") == "0:0",
    }


def ionic_energy_points(text: str) -> list[list]:
    """Read complete OSZICAR ionic records, retaining only the latest restart."""
    points = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"\s*(\d+)\s+(?:T=.*?\s+)?F=\s*\S+\s+E0=\s*([-+.\dEeDd]+)", line)
        if not match or not line.endswith(("\n", "\r")):
            continue
        try:
            energy = float(match[2].replace("D", "E").replace("d", "e"))
        except ValueError:
            continue
        if math.isfinite(energy):
            step = int(match[1])
            if points and step <= points[-1][0]:
                points.clear()
            points.append([step, energy])
    return points


def selective_atoms(poscar: str) -> tuple[list[bool] | None, str]:
    """Exclude fully fixed atoms; partial directional constraints remain raw forces.

    Selective flags are in the lattice basis, so masking Cartesian force components
    would be incorrect for skew cells. Do not claim projected forces in that case.
    """
    lines = poscar.splitlines()
    try:
        count_line = 5 if all(v.isdigit() for v in lines[5].split()) else 6
        count = sum(int(v) for v in lines[count_line].split())
        start = count_line + 1
        if not lines[start].strip().lower().startswith("s"):
            return [True] * count, "all_atoms"
        flags = [line.split()[3:6] for line in lines[start + 2 : start + 2 + count]]
        if len(flags) != count or any(len(row) != 3 for row in flags):
            return None, "raw"
        flags = [[value.upper() for value in row] for row in flags]
        if any(value not in {"T", "F"} for row in flags for value in row):
            return None, "raw"
        if any(len(set(row)) != 1 for row in flags):
            return [True] * count, "raw_directional_constraints"
        return [row[0] == "T" for row in flags], "free_atoms"
    except (ValueError, IndexError):
        return None, "raw"


def vasp_convergence(outcar: str, incar: str, poscar: str, truncated: bool = False) -> dict:
    """Parse bounded OUTCAR records without inventing force steps or convergence."""
    active, basis = selective_atoms(poscar)
    forces, step, rows, in_block = [], None, [], False
    converged = False
    for line in outcar.splitlines(keepends=True):
        if not line.endswith(("\n", "\r")):
            break
        iteration = re.search(r"\bIteration\s+(\d+)\s*\(\s*\d+\s*\)", line)
        if iteration:
            next_step = int(iteration[1])
            if step is not None and next_step < step:
                forces.clear()
                converged = False
            step = next_step
            in_block = False
        if "reached required accuracy" in line and "stopping structural" in line:
            converged = True
        if "TOTAL-FORCE (eV/Angst)" in line:
            in_block, rows = True, []
            continue
        if not in_block:
            continue
        if re.fullmatch(r"\s*-+\s*", line):
            if not rows:
                continue
            if step is not None and (active is None or len(rows) == len(active)):
                norms = [
                    math.sqrt(sum(v * v for v in row))
                    for index, row in enumerate(rows)
                    if active is None or active[index]
                ]
                if norms and all(math.isfinite(value) for value in norms):
                    forces.append([step, max(norms)])
            in_block = False
            continue
        try:
            values = [float(value.replace("D", "E")) for value in line.split()]
            if len(values) != 6 or not all(math.isfinite(value) for value in values):
                raise ValueError("incomplete force row")
            rows.append(values[3:])
        except ValueError:
            in_block = False
    parameters = {}
    # OUTCAR contains effective values; INCAR is the fallback before output exists.
    clean_incar = "\n".join(re.split(r"[!#]", line)[0] for line in incar.splitlines())
    for name in ("NSW", "IBRION", "EDIFFG", "POTIM", "TEBEG", "TEEND"):
        matches = re.findall(rf"\b{name}\s*=\s*([-+\d.eEdD]+)", clean_incar + "\n" + outcar, re.I)
        if matches:
            try:
                value = float(matches[-1].replace("D", "E").replace("d", "e"))
                if math.isfinite(value):
                    parameters[name.lower()] = int(value) if name in {"NSW", "IBRION"} else value
            except ValueError:
                pass
    return {
        "force_points": forces,
        "force_basis": basis,
        "ionic_converged": converged,
        "outcar_available": bool(outcar),
        "force_history_truncated": truncated,
        **parameters,
    }


def md_number(value: str) -> float | None:
    try:
        number = float(value.replace("D", "E").replace("d", "e"))
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def vasp_md(oszicar: str, monitor: dict) -> dict:
    """OSZICAR T/E/F/E0/EK are distinct observables, not relaxation convergence."""
    points = []
    for line in oszicar.splitlines(keepends=True):
        match = re.match(r"\s*(\d+)\s+T\s*=", line)
        if not match or not line.endswith(("\n", "\r")):
            continue
        values = {
            key: md_number(value)
            for key, value in re.findall(r"\b(T|E|F|E0|EK|SP|SK)\s*=\s*([^\s]+)", line)
        }
        if values.get("T") is None or values.get("E0") is None:
            continue
        step = int(match[1])
        if points and step <= points[-1]["step"]:
            points.clear()
        point = {"step": step, "temperature": values["T"], "energy": values["E0"]}
        for original, key in (("F", "potential"), ("E", "total"), ("EK", "kinetic")):
            if values.get(original) is not None:
                point[key] = values[original]
        if monitor.get("potim", 0) > 0:
            point["time_ps"] = step * monitor["potim"] / 1000
        points.append(point)
    if not points and monitor.get("ibrion") != 0:
        return {}
    return {
        "kind": "md",
        "engine": "VASP",
        "md_points": points,
        "energy_unit": "eV",
        "temperature_unit": "K",
        "target_temperature": monitor.get("tebeg")
        if monitor.get("tebeg") == monitor.get("teend", monitor.get("tebeg"))
        else None,
        "total_steps": monitor.get("nsw"),
        "dt_fs": monitor.get("potim"),
        "energy_labels": {"potential": "F", "total": "E (OSZICAR)", "energy": "E0"},
    }


def lammps_md(log: str, inputs: str = "") -> dict:
    """Read standard/custom thermo tables, including sparse samples and run restarts."""
    complete = [line for line in log.splitlines(keepends=True) if line.endswith(("\n", "\r"))]
    setup = inputs + "\n" + "".join(complete)
    setup = "\n".join(line.split("#", 1)[0] for line in setup.splitlines())
    styles = re.findall(r"^\s*units\s+(\w+)", setup, re.M)
    style = styles[-1] if styles else None
    timesteps = re.findall(r"^\s*timestep\s+([-+\d.eEdD]+)", setup, re.M)
    dt = md_number(timesteps[-1]) if timesteps else None
    if dt is None:
        matches = re.findall(r"Time step\s*:\s*([-+\d.eEdD]+)", setup)
        dt = md_number(matches[-1]) if matches else None
    time_factor = {
        "metal": 1.0,
        "real": 0.001,
        "electron": 0.001,
        "si": 1e12,
        "cgs": 1e12,
        "micro": 1e6,
        "nano": 1e3,
    }.get(style)
    energy_unit = {
        "metal": "eV",
        "real": "kcal/mol",
        "electron": "Hartree",
        "si": "J",
        "cgs": "erg",
        "lj": "ε",
    }.get(style, "原生单位")
    pressure_unit = {
        "metal": "bar",
        "real": "atm",
        "electron": "Pa",
        "si": "Pa",
        "cgs": "dyn/cm²",
        "lj": "ε/σ³",
    }.get(style, "原生单位")
    temperature_unit = "K" if style and style != "lj" else "T*" if style == "lj" else "原生单位"
    mapping = {
        "Temp": "temperature",
        "PotEng": "potential",
        "TotEng": "total",
        "KinEng": "kinetic",
        "Press": "pressure",
        "Time": "time",
        "Dt": "dt",
    }
    temperature_computes = re.findall(r"^\s*compute\s+(\S+)\s+\S+\s+temp(?:\s|$)", setup, re.M)
    for name in temperature_computes:
        mapping[f"c_{name}"] = "temperature"
    temperature_columns = {name for name, key in mapping.items() if key == "temperature"}
    variable_time = bool(re.search(r"\bdt/reset\b|^\s*reset_timestep\b", setup, re.M))
    # Do not infer elapsed time across changing timestep commands. An explicit
    # thermo Time column remains authoritative for these runs.
    variable_time |= len(set(timesteps)) > 1
    header, points = [], []
    run_count, final_step, run_start = None, None, None
    stage = "md"
    for line in complete:
        command = re.match(r"\s*(run|minimize)\s+(.*)", line)
        if command:
            header = []
            stage = "md" if command[1] == "run" else "minimization"
            if command[1] == "run":
                run = re.fullmatch(r"(\d+)(?:\s+(?:pre|post)\s+(?:yes|no))*\s*", command[2])
                run_count = int(run[1]) if run else None
                run_start, final_step = None, None
            else:
                run_count, run_start, final_step = None, None, None
            continue
        cells = line.split()
        if stage == "minimization":
            continue
        if (
            "Step" in cells
            and temperature_columns.intersection(cells)
            and all(md_number(v) is None for v in cells)
        ):
            header = cells
            continue
        if not header or not cells:
            continue
        if line.lstrip().startswith(("Loop time", "ERROR", "LAMMPS", "run ", "minimize ")):
            header = []
            continue
        if len(cells) != len(header):
            continue
        values = [md_number(value) for value in cells]
        row = dict(zip(header, values, strict=True))
        step = row.get("Step")
        if step is None or step < 0 or not step.is_integer():
            continue
        point = {"step": int(step)}
        if run_count is not None and run_start is None:
            run_start = int(step)
            final_step = run_start + run_count
        point.update({key: row[name] for name, key in mapping.items() if row.get(name) is not None})
        if "temperature" not in point:
            continue
        if points and step < points[-1]["step"]:
            points.clear()
        # LAMMPS repeats the boundary step at the next run; keep its newest record.
        if points and step == points[-1]["step"]:
            points.pop()
        if time_factor is not None:
            if "time" in point:
                point["time_ps"] = point["time"] * time_factor
            elif dt is not None and not variable_time:
                point["time_ps"] = step * dt * time_factor
        points.append(point)
    targets = re.findall(
        r"\b(?:nvt|npt|langevin)\s+(?:temp\s+)?([-+\d.eEdD]+)\s+([-+\d.eEdD]+)", setup
    )
    target = None
    if targets and md_number(targets[-1][0]) == md_number(targets[-1][1]):
        target = md_number(targets[-1][0])
    if stage == "minimization":
        points = []
    norms = re.findall(r"^\s*thermo_modify\s+[^\n]*?\bnorm\s+(yes|no)\b", setup, re.M)
    normalized = norms[-1] == "yes" if norms else style == "lj"
    if normalized:
        energy_unit += "/atom"
    return {
        "kind": "md",
        "engine": "LAMMPS",
        "md_points": points,
        "units_style": style,
        "energy_unit": energy_unit,
        "pressure_unit": pressure_unit,
        "temperature_unit": temperature_unit,
        "target_temperature": target,
        "dt_fs": dt * time_factor * 1000 if dt is not None and time_factor else None,
        "run_steps": run_count,
        "run_target_step": final_step,
        "stage": stage,
    }


def vasp_snapshot(root: Path, requested: str = "info") -> dict:
    """Bounded read-only snapshots; never read potentials or recurse into directories."""
    structures = ("POSCAR", "CONTCAR", "XDATCAR")
    scripts = ("submit.sh", "run.slurm", "job.sh", "_chem_submit.sh")
    names = (*structures, "INCAR", "KPOINTS", "OSZICAR", "OUTCAR", *scripts)
    if requested != "info" and requested not in structures:
        raise ValueError("Unknown structure file")
    files, contents = {}, {}
    for name in names:
        path = safe_file(root, name)
        if not path.is_file() or not path.stat().st_size:
            continue
        files[name] = path.stat().st_size
        if requested == name or (requested == "info" and name not in (*structures, "OUTCAR")):
            limit = 32 * 1024 * 1024 if name in structures else 2 * 1024 * 1024
            with path.open("rb") as stream:
                if name == "OSZICAR":
                    stream.seek(max(0, files[name] - limit))
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"{name} exceeds online preview limit")
            contents[name] = data.decode("utf-8", errors="replace")
    result = {"files": files, "contents": contents}
    if requested == "info":
        outcar, poscar = "", ""
        limit = VASP_OUTCAR_LIMIT
        if "OUTCAR" in files:
            with safe_file(root, "OUTCAR").open("rb") as stream:
                offset = max(0, files["OUTCAR"] - limit)
                stream.seek(offset)
                if offset:
                    stream.readline()  # Never interpret a partial first record.
                outcar = stream.read(limit).decode("utf-8", errors="replace")
        if 0 < files.get("POSCAR", 0) <= 512 * 1024:
            poscar = safe_file(root, "POSCAR").read_text(errors="replace")
        result["monitor"] = vasp_convergence(
            outcar, contents.get("INCAR", ""), poscar, files.get("OUTCAR", 0) > limit
        )
        result["monitor"].update(vasp_md(contents.get("OSZICAR", ""), result["monitor"]))
        result["structure_files"] = [name for name in structures if name in files]
    return result


def scientific_snapshot(root: Path, requested: str = "info") -> dict:
    """Discover MD logs and structures in the job directory without reading potentials."""
    entries = sorted(
        path.name for path in root.iterdir() if path.is_file() and not path.is_symlink()
    )
    logs = [
        name
        for name in entries
        if name in {"log.lammps", "lammps.log"}
        or name.startswith("log.")
        or name.endswith((".log", ".log.lammps"))
    ]
    inputs = [
        name
        for name in entries
        if name not in logs
        and (name.startswith("in.") or name.endswith((".lmp", ".in", ".lammps", ".lammps.input")))
    ]
    structures = [
        name
        for name in entries
        if name in {"POSCAR", "CONTCAR", "XDATCAR"}
        or name.endswith((".xyz", ".extxyz", ".lammpstrj", ".dump", ".vasp"))
    ]
    if "INCAR" in entries or not (inputs or logs):
        if requested == "info" or requested in {"POSCAR", "CONTCAR", "XDATCAR"}:
            return vasp_snapshot(root, requested)
    if requested != "info":
        if requested not in structures:
            raise ValueError("Unknown structure file")
        path = safe_file(root, requested)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Structure exceeds online snapshot limit (32 MiB)")
        return {
            "files": {requested: path.stat().st_size},
            "contents": {requested: path.read_text(errors="replace")},
        }
    files, contents = {}, {}
    for name in [
        *inputs,
        *logs,
        *structures,
        "submit.sh",
        "run.slurm",
        "job.sh",
        "_chem_submit.sh",
    ]:
        path = safe_file(root, name)
        if not path.is_file() or not path.stat().st_size:
            continue
        size = path.stat().st_size
        files[name] = size
        if name in structures:
            continue
        limit = 16 * 1024 * 1024 if name in logs else 512 * 1024
        if name not in logs and size > limit:
            continue
        with path.open("rb") as stream:
            if size > limit:
                stream.seek(size - limit)
                stream.readline()
            contents[name] = stream.read(limit).decode("utf-8", errors="replace")
    input_text = "\n".join(contents.get(name, "") for name in inputs)
    candidates = []
    for name in logs:
        text = contents.get(name, "")
        if "LAMMPS" not in text and not re.search(r"^\s*Step\s+.*\b(?:Temp|c_\w+)\b", text, re.M):
            continue
        monitor = lammps_md(text, input_text)
        monitor.update(source_file=name, history_truncated=files.get(name, 0) > 16 * 1024 * 1024)
        candidates.append(monitor)
    monitor = max(
        candidates,
        key=lambda m: (m["source_file"] == "log.lammps", len(m["md_points"])),
        default=lammps_md("", input_text),
    )
    trajectory = next((name for name in structures if name.endswith((".lammpstrj", ".dump"))), None)
    if not trajectory:
        trajectory = next(
            (name for name in structures if re.search(r"traj|md|dump", name, re.I)), None
        )
    monitor["trajectory_file"] = trajectory
    log_text = contents.get(monitor.get("source_file"), "")
    metadata = "\n".join(
        line
        for line in log_text.splitlines()
        if re.match(
            r"^\s*(?:units|boundary|mass|labelmap|dump_modify|pair_coeff|atom_style)\s", line
        )
    )
    companions = [{"name": "trajectory-metadata.in", "text": metadata}]
    data_files = re.findall(r"^\s*read_data\s+[\"']?([^\s\"']+)", log_text, re.M)
    if data_files:
        try:
            path = safe_file(root, data_files[-1])
            with path.open("rb") as stream:
                header = stream.read(512 * 1024).decode("utf-8", errors="replace")
            header = re.split(r"(?m)^Atoms(?:\s|$)", header)[0]
            companions.append({"name": "trajectory-masses.data", "text": header})
        except (ValueError, OSError):
            pass
    return {
        "files": files,
        "contents": contents,
        "monitor": monitor,
        "structure_companions": companions,
        "parameter_files": inputs,
        "structure_files": [name for name in structures if name in files],
    }


def dispatch(request: dict) -> dict:
    root = Path(request["root"]).expanduser().resolve()
    if "archive" in root.parts:
        raise ValueError("不能在 archive 目录操作。")
    action = request["action"]
    if action == "result_snapshot":
        return result_snapshot(
            root, request["source"], request.get("file"), request.get("path_step")
        )
    if action == "vasp_snapshot":
        return vasp_snapshot(root, request.get("file", "info"))
    if action == "scientific_snapshot":
        return scientific_snapshot(root, request.get("file", "info"))
    if action in {"observe_external", "cancel_external"}:
        observed = external_job(request)
        if action == "cancel_external":
            if not request.get("identity") or not observed.get("known"):
                return {"cancel_requested": False, "error": "作业身份未确认，不取消。"}
            if observed["terminal"]:
                return {"cancel_requested": False, "already_terminal": True}
            result = run(["scancel", request["scheduler_job_id"]])
            return {"cancel_requested": result.returncode == 0, "error": result.stderr[-4000:]}
        return observed
    if action == "environment":
        queues = run(["sinfo", "-h", "-o", "%P|%a|%l|%G"])
        return {
            "user": pwd.getpwuid(os.getuid()).pw_name,
            "queues": queues.stdout,
            "error": (queues.stderr or f"sinfo exited with {queues.returncode}")
            if queues.returncode
            else "",
            "stderr": queues.stderr,
            "ready": queues.returncode == 0,
            "python": sys.version.split()[0],
        }
    if action == "submit":
        return submit(root, request)
    if action == "status":
        return status(root)
    if action == "copy":
        import hashlib
        import shutil

        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (root / "_chem_receipt.json").exists():
            return {"sealed": True}
        source_root = Path(request["source_root"]).expanduser().resolve()
        if "archive" in source_root.parts:
            raise ValueError("不能读取 archive。")
        source = safe_file(source_root, request["source"])
        target = safe_file(root, request["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, target.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1048576)
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
        return {"size": target.stat().st_size, "sha256": digest.hexdigest()}
    if action == "fingerprint":
        import hashlib

        source = safe_file(root, request["path"])
        before = source.stat()
        hasher = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("File changed while hashing; wait for a stable output")
        return {"size": after.st_size, "sha256": digest}
    if action == "upload":
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = safe_file(root, request["path"])
        # Upload only before the remote submission reservation, never alter live inputs.
        if (root / "_chem_receipt.json").exists():
            return {"sealed": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb" if request["offset"] == 0 else "r+b") as stream:
            stream.seek(request["offset"])
            stream.write(base64.b64decode(request["data"], validate=True))
        return {"written": True}
    if action == "read":
        path = safe_file(root, request["path"])
        with path.open("rb") as stream:
            stream.seek(request.get("offset", 0))
            data = stream.read(min(request.get("length", 65536), 1048576))
        return {"data": base64.b64encode(data).decode(), "size": path.stat().st_size}
    if action == "files":
        entries = []
        # Walk explicitly so archive is never traversed; resolve each file before returning.
        import fnmatch

        pending = [root]
        while pending:
            for path in pending.pop().iterdir():
                if path.name == "archive" or path.is_symlink():
                    continue
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    relative = str(path.relative_to(root))
                    if any(
                        fnmatch.fnmatchcase(relative, pattern) for pattern in request["patterns"]
                    ):
                        entries.append({"path": relative, "size": path.stat().st_size})
        return {"files": entries}
    if action == "cancel":
        receipt = json.loads((root / "_chem_receipt.json").read_text())
        if not receipt.get("scheduler_job_id"):
            return {"cancelled": False, "status": receipt["status"]}
        result = run(["scancel", receipt["scheduler_job_id"]])
        return {"cancel_requested": result.returncode == 0, "error": result.stderr[-4000:]}
    raise ValueError("Unknown remote operation")


def main() -> None:
    try:
        result = dispatch(json.load(sys.stdin))
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
