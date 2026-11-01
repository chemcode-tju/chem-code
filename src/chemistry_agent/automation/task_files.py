"""Read/stage arbitrary files within a calculation's retrieved or remote directory."""

import os
import shlex
import shutil
from pathlib import Path, PurePosixPath

from chemistry_agent.errors import AgentError


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"..", "archive"} for part in path.parts)
        or "\x00" in value
    ):
        raise AgentError("invalid_task_path", "请使用计算目录内的相对路径。", 422)
    return str(path)


def remote_path(transport, root: str, name: str) -> str:
    base = transport.normalize(root)
    target = transport.normalize(str(PurePosixPath(base) / relative_path(name)))
    if target != base and not target.startswith(base.rstrip("/") + "/"):
        raise AgentError("invalid_task_path", "文件链接指向计算目录之外。", 422)
    return target


def task_files(payload: dict) -> dict:
    from chemistry_agent.automation.backend import load_job

    node = load_job(payload["job_id"])
    name = relative_path(payload.get("path", "."))
    source = payload.get("source", "retrieved")
    action = payload.get("mode", "list")
    offset, length = int(payload.get("offset", 0)), int(payload.get("length", 12000))
    if offset < 0 or not 1 <= length <= 64000:
        raise AgentError("invalid_file_range", "读取范围无效，请分段读取。", 422)
    if source == "retrieved":
        if "retrieved" not in node.outputs:
            raise AgentError("task_files_pending", "文件尚未取回，可查询 remote 目录。", 409)
        repo = node.outputs.retrieved.base.repository
        if action == "list":
            entries = repo.list_objects(None if name == "." else name)
            return {
                "source": source,
                "path": name,
                "entries": [
                    {"name": entry.name, "directory": entry.is_dir()}
                    for entry in entries
                    if entry.name != "archive"
                ],
            }
        with repo.open(name, mode="rb") as stream:
            stream.seek(offset)
            data = stream.read(length + 1)
    else:
        if "remote_folder" not in node.outputs:
            raise AgentError("task_files_pending", "远端任务目录尚未创建。", 409)
        folder = node.outputs.remote_folder
        with folder.computer.get_transport() as transport:
            target = remote_path(transport, folder.get_remote_path(), name)
            if action == "list":
                entries = [
                    {"name": entry, "directory": transport.isdir(f"{target}/{entry}")}
                    for entry in transport.listdir(target)
                    if entry != "archive"
                ]
                return {"source": source, "path": name, "entries": entries}
            command = (
                f"dd if={shlex.quote(target)} bs=1 skip={offset} count={length + 1} 2>/dev/null"
            )
            code, output, error = transport.exec_command_wait_bytes(command)
            if code:
                raise AgentError("task_file_unreadable", "无法读取该任务文件。", 422)
            data = output if isinstance(output, bytes) else output.encode()
    return {
        "source": source,
        "path": name,
        "offset": offset,
        "next_offset": offset + length if len(data) > length else None,
        "content": data[:length].decode("utf-8", errors="replace"),
        "note": "文本片段；二进制或大文件请用 calculation_analyze 在本地读取。",
    }


def stage_files(payload: dict) -> dict:
    """Copy files straight to the analysis workspace, without a size cap or LLM transfer."""
    from chemistry_agent.automation.backend import load_job

    node = load_job(payload["job_id"])
    workspace = Path(os.environ["AIIDA_PATH"]).resolve() / "chat"
    requested = Path(payload["directory"])
    if any(part.is_symlink() for part in [requested, *requested.parents] if part != workspace):
        raise AgentError("invalid_analysis_directory", "分析目录不能使用符号链接。", 422)
    target = requested.resolve()
    if not target.is_relative_to(workspace):
        raise AgentError("invalid_analysis_directory", "分析目录须位于会话工作区。", 422)
    target.mkdir(parents=True, exist_ok=True)
    names = payload["files"]
    source = payload.get("source", "retrieved")
    manifest = {}
    if source == "retrieved":
        if "retrieved" not in node.outputs:
            raise AgentError("task_files_pending", "文件尚未取回，可使用 remote 来源。", 409)
        repo = node.outputs.retrieved.base.repository
        if names == ["*"]:
            names = [
                str(PurePosixPath(root) / name)
                for root, _, files in repo.walk()
                for name in files
                if "archive" not in PurePosixPath(root, name).parts
            ]
        for name in names:
            name = relative_path(name)
            destination = target / name
            if not destination.resolve().is_relative_to(target):
                raise AgentError("invalid_task_path", "输出链接越过分析目录。", 422)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with repo.open(name, mode="rb") as stream, destination.open("wb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
            except FileNotFoundError as exc:
                raise AgentError(
                    "retrieved_file_missing",
                    f"任务 {node.pk} 取回目录中缺少 {name}。用 calculation_files 检查 remote "
                    "目录；若远端有原文件，可将本分析节点 file_source 改为 remote。",
                    422,
                ) from exc
            manifest[name] = str(destination)
    else:
        if "remote_folder" not in node.outputs:
            raise AgentError("task_files_pending", "远端任务目录尚未创建。", 409)
        folder = node.outputs.remote_folder
        with folder.computer.get_transport() as transport:
            root = remote_path(transport, folder.get_remote_path(), ".")
            if names == ["*"]:
                # Recursion deliberately excludes archive; all other file types are accepted.
                pending, names, visited = ["."], [], set()
                while pending:
                    current = pending.pop()
                    canonical = remote_path(transport, root, current)
                    if canonical in visited:
                        continue
                    visited.add(canonical)
                    for entry in transport.listdir(canonical):
                        if entry == "archive":
                            continue
                        name = str(PurePosixPath(current) / entry)
                        remote = remote_path(transport, root, name)
                        if transport.isdir(remote):
                            pending.append(name)
                        else:
                            names.append(name)
            for name in names:
                name = relative_path(name)
                remote = remote_path(transport, root, name)
                destination = target / name
                if not destination.resolve().is_relative_to(target):
                    raise AgentError("invalid_task_path", "输出链接越过分析目录。", 422)
                destination.parent.mkdir(parents=True, exist_ok=True)
                transport.getfile(remote, str(destination))
                manifest[name] = str(destination)
    return {"files": manifest, "source": source, "job_id": node.uuid}
