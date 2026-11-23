"""Standard-library operations executed on the scheduler host."""

from __future__ import annotations

import base64
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from calendar import monthrange
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

QUEUE_ACTIONS = {
    "queue",
    "queue_history",
    "queue_dashboard",
    "queue_dashboard_snapshot",
    "queue_root",
    "queue_browse",
    "queue_metadata",
    "queue_dump_frame",
}
TERMINAL_STATES = {
    "COMPLETED",
    "CANCELLED",
    "FAILED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "BOOT_FAIL",
    "DEADLINE",
    "PREEMPTED",
    "REVOKED",
}
HISTORY_LIMIT = 300
DUMP_SUFFIXES = {".dump", ".lammpstrj"}
JOB_ID = r"^[0-9]+(?:[_+][0-9]+)?$"
CHUNK = 8 * 1024 * 1024
ACCOUNTING_EPOCH = date(1970, 1, 2)
BOARD_MONTHS = {"month": 1, "half-year": 6, "year": 12}
ACTIVE_GROUPS = {
    "RUNNING": "running",
    "COMPLETING": "running",
    "RESIZING": "running",
    "STAGE_OUT": "running",
    "PENDING": "queued",
    "CONFIGURING": "queued",
    "REQUEUED": "queued",
    "REQUEUE_HOLD": "queued",
}


class DashboardAllocation(NamedTuple):
    status: str
    ended: str
    cpu_seconds: str
    elapsed_seconds: str
    resources: str
    nodes: str = ""


def queue_entries(job_id: str = "") -> list[dict]:
    """Use the cluster account and Slurm's working directory, independent of chat."""
    owner = pwd.getpwuid(os.getuid()).pw_name
    fields = ("id", "owner", "partition", "status", "elapsed", "submitted", "directory", "name")
    separator = "\x1f"
    command = [
        "squeue",
        "--local",
        "-ahr",
        "-u",
        owner,
        "-t",
        "all",
        "-o",
        separator.join(("%i", "%u", "%P", "%T", "%M", "%V", "%Z", "%j")),
    ]
    if job_id:
        if not re.fullmatch(JOB_ID, job_id):
            raise ValueError("作业编号无效。")
        command.append(f"--jobs={job_id.split('+', 1)[0]}")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=dict(os.environ, SLURM_TIME_FORMAT="standard"),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Slurm 队列查询超时。") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Slurm 队列查询失败。")
    jobs = []
    for line in result.stdout.splitlines():
        values = line.split(separator, len(fields) - 1)
        if len(values) != len(fields):
            continue
        row = dict(zip(fields, values))  # noqa: B905 - lengths checked; supports Python 3.9.
        if row["owner"] != owner or not re.fullmatch(JOB_ID, row["id"]):
            continue
        if row["status"] in TERMINAL_STATES:
            continue
        if not job_id or row["id"] == job_id:
            jobs.append(row)
    return jobs


def queue_partitions() -> dict:
    """List configured partitions, including ones without the user's jobs."""
    try:
        result = subprocess.run(
            ["sinfo", "-ah", "-o", "%R"], capture_output=True, text=True, timeout=8, check=True
        )
        return {"partitions": sorted(set(result.stdout.split()))}
    except (OSError, subprocess.SubprocessError):
        return {"partition_error": "分区列表查询失败，仍可查看作业所属分区。"}


def queue_history(job_id: str = "") -> list[dict]:
    owner = pwd.getpwuid(os.getuid()).pw_name
    fields = (
        "id",
        "owner",
        "partition",
        "status",
        "elapsed",
        "submitted",
        "ended",
        "directory",
        "name",
    )
    # Both older and newer supported Slurm versions offer the parsable accounting format.
    command = [
        "sacct",
        "-X",
        "-n",
        "-P",
        "--local",
        "-u",
        owner,
        "-S",
        "now-7days",
        "-E",
        "now",
        "--format=JobID,User,Partition,State%50,Elapsed,Submit,End,WorkDir,JobName",
    ]
    if job_id:
        if not re.fullmatch(JOB_ID, job_id):
            raise ValueError("作业编号无效。")
        command.append(f"--jobs={job_id.split('+', 1)[0]}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("此服务器暂无法从 Slurm 读取历史作业。") from exc
    jobs = []
    for line in result.stdout.splitlines():
        values = line.split("|", len(fields) - 1)
        if len(values) != len(fields):
            continue
        row = dict(zip(fields, values))  # noqa: B905 - lengths checked; supports Python 3.9.
        if row["owner"] != owner or not re.fullmatch(JOB_ID, row["id"]):
            continue
        row["status"] = row["status"].split()[0].rstrip("+") if row["status"] else ""
        if row["status"] in TERMINAL_STATES and (not job_id or job_id == row["id"]):
            jobs.append(row)
    return sorted(jobs, key=lambda row: row["ended"], reverse=True)


def dashboard_accounting(start: str) -> dict[tuple[str, str], DashboardAllocation]:
    """Read allocation records for the scheduler account, with no job-list row limit."""
    owner = pwd.getpwuid(os.getuid()).pw_name
    command = [
        "sacct",
        "-X",
        "-n",
        "-P",
        "--local",
        "--duplicates",
        "-u",
        owner,
        "-S",
        start,
        "-E",
        "now",
        "--format=JobID%100,User,State%50,Submit,End,CPUTimeRAW,ElapsedRaw,AllocTRES%512,NodeList%512",
    ]
    options = {
        "capture_output": True,
        "text": True,
        "timeout": 20,
        "check": True,
        "env": dict(os.environ, LC_ALL="C", SLURM_TIME_FORMAT="standard"),
    }
    try:
        result = subprocess.run(["sacct", "--array", *command[1:]], **options)
    except subprocess.CalledProcessError as exc:
        # Slurm 22.05 emits individual accounted array tasks but has no --array option.
        error = exc.stderr or ""
        if not re.search(r"(?:unrecognized|unknown|invalid) option[^\n]*--array\b", error):
            raise
        result = subprocess.run(command, **options)
    jobs = {}
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 9:
            continue
        identifier, user, state, submitted, ended, cpu_seconds, elapsed, resources, nodes = fields
        if user != owner or not re.fullmatch(JOB_ID, identifier):
            continue
        status = state.split(" ", 1)[0].rstrip("+")
        key = identifier, submitted
        # A resize can share the submission timestamp with the current/terminal record.
        if key not in jobs or status != "RESIZING":
            jobs[key] = DashboardAllocation(status, ended, cpu_seconds, elapsed, resources, nodes)
    return jobs


def gpu_accounting_configured() -> bool:
    """A CPU-only allocation implies zero GPUs when GPU accounting is enabled."""
    try:
        result = subprocess.run(
            ["scontrol", "show", "config"], capture_output=True, text=True, timeout=4, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"^AccountingStorageTRES[ \t]*=[ \t]*(.*)$", result.stdout, re.MULTILINE)
    return bool(match and "gres/gpu" in (item.strip() for item in match[1].split(",")))


def allocated_gpus(resources: str, tracked: bool) -> dict[str | None, int] | None:
    """Keep typed counts inside the aggregate limit and preserve untyped GPUs."""
    fields = dict(item.split("=", 1) for item in resources.split(",") if "=" in item)
    typed = {
        name.split(":", 1)[1]: value
        for name, value in fields.items()
        if name.startswith("gres/gpu:")
    }
    typed_valid = all(re.fullmatch(r"[0-9]+", value) for value in typed.values())
    counts: dict[str | None, int] = {}
    if typed_valid:
        counts.update({model: int(value) for model, value in typed.items() if int(value)})
    if "gres/gpu" in fields:
        raw = fields["gres/gpu"]
        if not re.fullmatch(r"[0-9]+", raw):
            return None
        total = int(raw)
        if sum(counts.values()) > total:
            counts.clear()
        remaining = total - sum(counts.values())
        if remaining:
            counts[None] = remaining
        return counts
    if typed:
        return counts if typed_valid else None
    return {} if tracked and re.fullmatch(r"[0-9]+", fields.get("cpu", "")) else None


def gpu_node_models() -> dict[str, str]:
    """Return the unambiguous model of each single-model node in current Slurm config."""
    try:
        result = subprocess.run(
            ["sinfo", "-N", "-h", "-o", "%N|%G"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    models_by_node: dict[str, set[str | None]] = {}
    pattern = r"(?:^|,)gpu:(?:([^:,()\s]+):)?[0-9]+(?=[(,]|$)"
    for line in result.stdout.splitlines():
        node, _, gres = line.strip().partition("|")
        models = {(model or None) for model in re.findall(pattern, gres)}
        models_by_node.setdefault(node, set()).update(models or {None})
    return {
        node: model
        for node, models in models_by_node.items()
        if len(models) == 1
        for model in models
        if model is not None
    }


def scheduler_time(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def months_before(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def board_start(today: date, period: str) -> date | None:
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=6)
    if period in BOARD_MONTHS:
        return months_before(today, BOARD_MONTHS[period]) + timedelta(days=1)
    if period == "all":
        return None
    raise ValueError("作业看板的时间范围无效。")


def accounting_retention_start(observed: datetime) -> date | None:
    """Read the retention boundary for complete dates, when visible to the account."""
    try:
        result = subprocess.run(
            ["sacctmgr", "-n", "show", "configuration"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
            env=dict(os.environ, LC_ALL="C"),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(
        r"^PurgeJobAfter[ \t]*=[ \t]*([0-9]+)[ \t]*(months?|days?|hours?)\b",
        result.stdout,
        re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return None
    amount, unit = int(match[1]), match[2].lower()
    today = observed.date()
    if unit.startswith("month"):
        return months_before(today.replace(day=1), amount)
    if unit.startswith("day"):
        return today - timedelta(days=amount)
    cutoff = observed.replace(minute=0, second=0, microsecond=0) - timedelta(hours=amount)
    return cutoff.date() + timedelta(days=bool(cutoff.hour))


def dashboard_snapshot(period: str) -> dict:
    """Read the account's allocation facts and the calendar interval queried from Slurm."""
    observed = datetime.now().astimezone()
    today = observed.date()
    requested_start = board_start(today, period)
    retention_start = accounting_retention_start(observed)
    if retention_start is not None:
        query_start = max(requested_start or ACCOUNTING_EPOCH, retention_start)
    else:
        # A wider query establishes an observed boundary even for an empty short window.
        query_start = ACCOUNTING_EPOCH if period == "all" else board_start(today, "year")
    try:
        entries = queue_entries()
    except (OSError, RuntimeError):
        entries = None
    try:
        accounting = dashboard_accounting(f"{min(query_start, today).isoformat()}T00:00:00")
    except (OSError, subprocess.SubprocessError):
        accounting = None
    coverage_start = None
    if accounting is not None:
        first_observed = retention_start or min(
            (
                submitted.date()
                for _, raw in accounting
                if (submitted := scheduler_time(raw)) and submitted.date() <= today
            ),
            default=None,
        )
        if first_observed is not None:
            coverage_start = max(first_observed, min(query_start, today))

    active = None
    gpu_tracked = accounting is not None and gpu_accounting_configured()
    node_models = None
    records = {}
    for (identifier, submitted), job in (accounting or {}).items():
        if not scheduler_time(submitted):
            continue
        model = None
        if (
            job.status in TERMINAL_STATES
            and job.nodes
            and (allocations := allocated_gpus(job.resources, gpu_tracked))
            and len(allocations) == 1
            and None in allocations
        ):
            if node_models is None:
                node_models = gpu_node_models()
            model = node_models.get(job.nodes)
        records[identifier, submitted] = {
            "id": identifier,
            "submitted": submitted,
            "status": job.status,
            "ended": job.ended,
            "cpu_seconds": job.cpu_seconds,
            "elapsed_seconds": job.elapsed_seconds,
            "resources": job.resources,
            "gpu_tracked": gpu_tracked,
            "inferred_gpu": model,
        }
    if entries is not None:
        active = {"running": 0, "queued": 0, "other": 0}
        seen = set()
        for job in entries:
            key = job["id"], job["submitted"]
            if key in seen:
                continue
            seen.add(key)
            active[ACTIVE_GROUPS.get(job["status"], "other")] += 1
            if scheduler_time(job["submitted"]):
                records.setdefault(
                    key,
                    {
                        "id": job["id"],
                        "submitted": job["submitted"],
                        "status": job["status"],
                        "ended": "",
                        "cpu_seconds": "",
                        "elapsed_seconds": "",
                        "resources": "",
                        "gpu_tracked": False,
                        "inferred_gpu": None,
                    },
                )
    return {
        "account": pwd.getpwuid(os.getuid()).pw_name,
        "observed_at": observed.isoformat(timespec="seconds"),
        "coverage_start": coverage_start.isoformat() if coverage_start else None,
        "active": active,
        "accounting_available": accounting is not None,
        "gpu_tracked": gpu_tracked,
        "records": list(records.values()),
    }


def calendar_dates(start: date, end: date) -> Iterator[date]:
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def dashboard_view(snapshot: dict, period: str, retained_days: set[str] | None = None) -> dict:
    """Aggregate one window, using only successfully queried calendar days."""
    observed = datetime.fromisoformat(snapshot["observed_at"])
    today = observed.date()
    live_start = snapshot["coverage_start"]
    known_dates = set(retained_days or ())
    if live_start:
        known_dates.update(
            day.isoformat() for day in calendar_dates(date.fromisoformat(live_start), today)
        )
    first = min((day for day in known_dates if day <= today.isoformat()), default=None)
    start = board_start(today, period) or (date.fromisoformat(first) if first else today)
    date_labels = [day.isoformat() for day in calendar_dates(start, today)]
    days = {
        label: {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "cpu_seconds": 0,
            "missing_cpu": 0,
            "gpu_seconds": 0,
            "missing_gpu": 0,
        }
        for label in date_labels
        if label in known_dates
    }
    hours = [0] * 24
    gpu_available = snapshot["gpu_tracked"]
    gpu_breakdown: dict[tuple[str | None, int, str], dict] = {}
    for job in snapshot["records"]:
        if (submitted := scheduler_time(job["submitted"])) and (
            day := days.get(submitted.date().isoformat())
        ):
            day["submitted"] += 1
            hours[submitted.hour] += 1
        allocations = allocated_gpus(job["resources"], job["gpu_tracked"])
        gpu_available |= allocations is not None
        if job["status"] not in TERMINAL_STATES or not (ended := scheduler_time(job["ended"])):
            continue
        if day := days.get(ended.date().isoformat()):
            category = {"COMPLETED": "completed", "CANCELLED": "cancelled"}.get(
                job["status"], "failed"
            )
            day[category] += 1
            if re.fullmatch(r"[0-9]+", job["cpu_seconds"]):
                day["cpu_seconds"] += int(job["cpu_seconds"])
            else:
                day["missing_cpu"] += 1
            if allocations == {} or job["elapsed_seconds"] == "0":
                continue
            if allocations is None or not re.fullmatch(r"[0-9]+", job["elapsed_seconds"]):
                day["missing_gpu"] += 1
            else:
                elapsed = int(job["elapsed_seconds"])
                day["gpu_seconds"] += sum(allocations.values()) * elapsed
                for model, cards in allocations.items():
                    source = "accounting" if model is not None else "unrecorded"
                    if model is None and len(allocations) == 1 and job["inferred_gpu"]:
                        model = job["inferred_gpu"]
                        source = "node"
                    entry = gpu_breakdown.setdefault(
                        (model, cards, source),
                        {
                            "model": model,
                            "cards": cards,
                            "source": source,
                            "elapsed_seconds": 0,
                            "gpu_seconds": 0,
                        },
                    )
                    entry["elapsed_seconds"] += elapsed
                    entry["gpu_seconds"] += cards * elapsed

    return {
        "observed_at": snapshot["observed_at"],
        "period": period,
        "period_start": start.isoformat(),
        "coverage_start": first,
        "active": snapshot["active"],
        "accounting_available": snapshot["accounting_available"] or bool(retained_days),
        "gpu_accounting_available": gpu_available,
        "gpu_breakdown": sorted(gpu_breakdown.values(), key=lambda row: -row["gpu_seconds"]),
        "days": [{"date": label} | days.get(label, {"submitted": None}) for label in date_labels],
        "submission_hours": hours,
    }


def queue_dashboard(period: str = "year") -> dict:
    return dashboard_view(dashboard_snapshot(period), period)


def queue_path(root: Path, relative: str) -> Path:
    """Reject hidden paths, traversal and links before inspecting job files."""
    path = Path(relative)
    if path.is_absolute() or any(
        part.startswith(".") or part.casefold() == "archive" for part in path.parts
    ):
        raise ValueError("只能浏览作业目录内的可见文件。")
    for part in path.parts:
        root = root / part
        if root.is_symlink():
            raise ValueError("无法浏览符号链接。")
    return root


def metadata_text(path: Path, limit: int, *, tail: bool = False) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return ""
        if tail and info.st_size > limit:
            stream.seek(info.st_size - limit)
            stream.readline(limit)
        return stream.read(limit).decode("utf-8", errors="replace")


def lammps_metadata(directory: Path, name: str) -> list[dict]:
    """Resolve literal input/data references within the current directory."""
    with os.scandir(directory) as entries:
        names = sorted(
            entry.name
            for entry in entries
            if not entry.name.startswith(".")
            and entry.name.casefold() != "archive"
            and entry.is_file(follow_symlinks=False)
        )
    names = names[:1000]
    stem = Path(name).stem
    inputs = [
        item
        for item in names
        if item != name
        and not item.lower().startswith("log")
        and (
            item.lower().startswith(("in.", "input."))
            or Path(item).suffix.lower() in {".in", ".inp", ".lammps", ".init", ".settings"}
        )
    ]
    datafiles = [item for item in names if Path(item).suffix.lower() in {".data", ".lmp"}]
    logs = [
        item
        for item in names
        if item == "log.lammps" or item.startswith("log.") or item.endswith(".log")
    ]

    def read_input(filename: str, *, log: bool = False) -> str:
        try:
            return metadata_text(queue_path(directory, filename), 256 * 1024, tail=log)
        except (OSError, ValueError):
            return ""

    def paths(text: str, operation: str, index: int) -> list[str]:
        result = []
        for line in re.sub(r"&\s*\r?\n", " ", text).splitlines():
            if not line.lstrip().startswith(operation + " "):
                continue
            try:
                words = shlex.split(line, comments=True)
            except ValueError:
                continue
            if len(words) > index and not any(char in words[index] for char in "$*?[`"):
                result.append(words[index])
        return result

    preferred = {f"{stem}.in", f"{stem}.inp", f"in.{stem}", f"input.{stem}"}
    previews = {
        item: read_input(item)
        for item in sorted(inputs, key=lambda item: (item not in preferred, item))[:12]
    }
    included = {str(Path(path)) for text in previews.values() for path in paths(text, "include", 1)}
    matching = [
        item
        for item, text in previews.items()
        if any(Path(path).name == name for path in paths(text, "dump", 5))
    ]
    named = [item for item in previews if item in preferred]
    roots = [
        item
        for item in previews
        if item not in included and Path(item).suffix.lower() not in {".init", ".settings"}
    ]
    # An unrelated input can redefine species or units. Automatic discovery
    # only uses an exact reference, a named match or one unambiguous root.
    pending = [(item, False) for item in (matching or named or (roots if len(roots) == 1 else []))]
    if not pending:
        named_logs = [item for item in logs if item in {f"{stem}.log", f"log.{stem}"}]
        pending = [(item, True) for item in (named_logs or (logs if len(logs) == 1 else []))]
    directives = re.compile(
        r"^\s*(?:units|boundary|mass|labelmap|dump|dump_modify|pair_coeff|atom_style|include|read_data)\s"
    )
    seen, referenced, contents = set(), [], []
    while pending and len(seen) < 12:
        filename, log = pending.pop(0)
        if filename in seen:
            continue
        seen.add(filename)
        text = previews[filename] if filename in previews else read_input(filename, log=log)
        lines = []
        for line in re.sub(r"&\s*\r?\n", " ", text).splitlines():
            if directives.match(line):
                lines.append(line)
            if not re.match(r"^\s*(?:include|read_data)\s", line):
                continue
            try:
                operation, target, *_ = shlex.split(line, comments=True)
                if "$" in target or not queue_path(directory, target).is_file():
                    continue
            except (OSError, ValueError):
                continue
            if operation == "include":
                pending.insert(0, (target, False))
            else:
                referenced.append(target)
        if lines:
            contents.append({"name": filename, "text": "\n".join(dict.fromkeys(lines))})
    same_stem = [item for item in datafiles if Path(item).stem == stem]
    possible = list(dict.fromkeys(referenced)) or same_stem or datafiles
    if len(possible) == 1:
        filename = possible[0]
        try:
            text = metadata_text(queue_path(directory, filename), 512 * 1024)
            header = re.split(r"(?m)^Atoms(?:\s|$)", text, maxsplit=1)[0]
            if re.search(r"(?m)^\s*(?:Masses|Atom Type Labels)\b", header):
                contents.insert(0, {"name": filename, "text": header})
        except (OSError, ValueError):
            pass
    return contents


def latest_dump_frame(path: Path) -> dict:
    """Read a bounded tail and return one complete LAMMPS frame, even during writes."""
    if path.suffix.lower() not in DUMP_SUFFIXES:
        raise ValueError("仅 LAMMPS 文本轨迹支持末帧预览。")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("请选择普通文件。")
        prefix = stream.read(512)
        stream.seek(max(0, info.st_size - CHUNK))
        tail = stream.read(CHUNK)
    units = re.search(rb"(?m)^ITEM: UNITS\r?\n[^\r\n]+\r?\n", prefix)
    markers = list(re.finditer(rb"(?m)^ITEM: TIMESTEP\r?$", tail))
    for index in range(len(markers) - 1, -1, -1):
        start = markers[index].start()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(tail)
        frame = tail[start:end]
        atom_count = re.search(rb"(?m)^ITEM: NUMBER OF ATOMS\r?\n([0-9]+)\r?$", frame)
        atom_table = re.search(rb"(?m)^ITEM: ATOMS [^\r\n]+\r?\n", frame)
        if not atom_count or not atom_table or not frame.endswith(b"\n"):
            continue
        count = int(atom_count[1])
        lines = frame[atom_table.end() :].splitlines()
        if count and len(lines) >= count and all(line.strip() for line in lines[:count]):
            data = frame[: atom_table.end()] + b"\n".join(lines[:count]) + b"\n"
            if units:
                data = units[0] + data
            return {"data": base64.b64encode(data).decode()}
    raise ValueError("轨迹末尾暂未找到完整结构帧。")


def job_root(payload: dict) -> Path:
    jobs = (queue_history if payload.get("history") else queue_entries)(payload["job_id"])
    job = next((row for row in jobs if row["submitted"] == payload["submitted"]), None)
    if not job:
        raise ValueError("作业已离开当前队列，请刷新列表。")
    directory = Path(job["directory"])
    if not directory.is_absolute() or any(part.casefold() == "archive" for part in directory.parts):
        raise ValueError("该作业没有可浏览的工作目录。")
    root = directory.resolve(strict=True)
    if any(part.casefold() == "archive" for part in root.parts):
        raise ValueError("该作业没有可浏览的工作目录。")
    return root


def queue_operation(payload: dict) -> dict:
    action = payload["action"]
    if action == "queue_dashboard":
        return queue_dashboard(payload.get("period", "year"))
    if action == "queue_dashboard_snapshot":
        return dashboard_snapshot(payload.get("period", "all"))
    home = str(Path.home())
    if action == "queue":
        return {"jobs": queue_entries(), "home": home} | queue_partitions()
    if action == "queue_history":
        rows = queue_history()
        return {
            "jobs": rows[:HISTORY_LIMIT],
            "truncated": len(rows) > HISTORY_LIMIT,
            "home": home,
        } | queue_partitions()
    if action == "browser_metadata":
        root = Path(payload["directory"])
        if root.resolve(strict=True) != root or any(
            part.casefold() == "archive" for part in root.parts
        ):
            raise ValueError("无法访问此目录。")
        path = queue_path(root, payload["path"])
        return {"companions": dump_companions(path)}
    root = job_root(payload)
    if action == "queue_root":
        return {"directory": str(root)}
    path = queue_path(root, payload.get("path", ""))
    if action == "queue_metadata":
        return {"companions": dump_companions(path)}
    if action == "queue_dump_frame":
        return latest_dump_frame(path)
    raise ValueError("不支持的作业操作。")


def dump_companions(path: Path) -> list[dict]:
    if path.suffix.lower() not in DUMP_SUFFIXES or not path.is_file():
        raise ValueError("请选择目录中的 LAMMPS 文本轨迹。")
    return lammps_metadata(path.parent, path.name)


def main() -> None:
    try:
        result = queue_operation(json.load(sys.stdin))
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
