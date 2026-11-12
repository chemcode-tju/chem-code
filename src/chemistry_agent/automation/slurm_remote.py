"""Dependency-free helper sent over SSH; it is not installed on the cluster.

Exactly one JSON request/response per connection. A durable reservation and Slurm
job name reconcile an interrupted sbatch without issuing another submission.
"""

import base64
import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

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


def dispatch(request: dict) -> dict:
    root = Path(request["root"]).expanduser().resolve()
    if "archive" in root.parts:
        raise ValueError("不能在 archive 目录操作。")
    action = request["action"]
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
            "error": queues.stderr,
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
