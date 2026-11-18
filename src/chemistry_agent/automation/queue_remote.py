"""Standard-library operations executed on the scheduler host."""

import base64
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

QUEUE_ACTIONS = {
    "queue",
    "queue_history",
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
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
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
    root = job_root(payload)
    if action == "queue_root":
        return {"directory": str(root)}
    path = queue_path(root, payload.get("path", ""))
    if action == "queue_metadata":
        if path == root or path.suffix.lower() not in DUMP_SUFFIXES or not path.is_file():
            raise ValueError("请选择作业目录中的 LAMMPS 文本轨迹。")
        return {"companions": lammps_metadata(path.parent, path.name)}
    if action == "queue_dump_frame":
        return latest_dump_frame(path)
    raise ValueError("不支持的作业操作。")


def main() -> None:
    try:
        result = queue_operation(json.load(sys.stdin))
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
