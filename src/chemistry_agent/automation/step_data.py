"""Read the approved VASP step's inputs, ionic history and structure snapshots."""

import io
import math
import re
import shlex
from collections.abc import Iterator
from contextlib import contextmanager

from aiida import orm
from ase.io import iread, write

from chemistry_agent.errors import AgentError

FILE_LIMIT = 32 * 1024 * 1024
LOG_LIMIT = 4 * 1024 * 1024
ENERGY = re.compile(r"^\s*(\d+)\s+F=\s*\S+\s+E0=\s*([-+.\dEeDd]+)(?:\s|$)")


def ionic_history(log: str) -> list[list]:
    """Count completed ionic summaries only. Exclude a line still being written."""
    points = []
    for line in log.splitlines(keepends=True):
        if not line.endswith(("\n", "\r")):
            continue
        match = ENERGY.match(line)
        if match:
            try:
                energy = float(match[2].upper().replace("D", "E"))
            except ValueError:
                continue
            if math.isfinite(energy):
                points.append([int(match[1]), energy])
    return points


def complete_xdatcar(text: str, count: int) -> str:
    """Trim a concurrently written last frame or cell header from a live snapshot."""
    markers = list(re.finditer(r"(?im)^\s*(?:Direct|Cartesian) configuration\s*=.*?\n", text))
    for marker in reversed(markers):
        end = marker.end()
        for _ in range(count):
            newline = text.find("\n", end)
            if newline < 0:
                break
            end = newline + 1
        else:
            return text[:end]
    raise AgentError("step_no_frames", "正在写入第一帧，稍后刷新即可查看。", 409)


def locate_node(payload: dict) -> orm.CalcJobNode | None:
    if payload.get("reused_pk"):
        node = orm.load_node(pk=payload["reused_pk"])
    else:
        parent = orm.load_node(uuid=payload["root_id"])
        if isinstance(parent, orm.CalcJobNode):
            node = parent if payload["stage"] == "single" else None
        else:
            label = f"{parent.label}:{payload['stage']}"
            node = next((c for c in parent.called if c.label == label), None)
    return node if isinstance(node, orm.CalcJobNode) else None


def archive_text(repo, name: str, limit: int = FILE_LIMIT, *, tail: bool = False) -> str:
    with repo.open(name, mode="rb") as handle:
        if tail:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise AgentError("step_file_large", "文件超过在线查看上限（32 MiB）。", 413)
    return data.decode("utf-8", errors="replace")


def xml_trajectory(repo) -> str:
    with repo.open("vasprun.xml", mode="rb") as handle:
        handle.seek(0, 2)
        if handle.tell() > FILE_LIMIT:
            raise AgentError("step_file_large", "历史 XML 超过在线重建上限（32 MiB）。", 413)
        handle.seek(0)
        output = io.StringIO()
        for atoms in iread(handle, format="vasp-xml", index=":"):
            write(
                output,
                atoms,
                format="extxyz",
                columns=["symbols", "positions"],
                write_results=False,
            )
            if output.tell() > FILE_LIMIT:
                raise AgentError("step_file_large", "轨迹超过在线查看上限（32 MiB）。", 413)
    if not output.tell():
        raise AgentError("step_no_frames", "尚无完整的优化轨迹帧。", 404)
    return output.getvalue()


@contextmanager
def remote_reader(node: orm.CalcJobNode) -> Iterator:
    """Run fixed, size-limited reads only in the work directory recorded by AiiDA."""
    directory = node.get_remote_workdir()
    if not directory:
        yield None
        return
    with node.computer.get_transport() as transport:

        def read(command: str) -> str:
            code, stdout, _ = transport.exec_command_wait(
                f"cd {shlex.quote(directory)} && {command}"
            )
            if code:
                raise AgentError("step_remote_read", "暂时无法读取此作业的远端文件。", 503)
            return stdout

        yield read


def step_data(payload: dict) -> dict:
    node = locate_node(payload)
    requested = payload["file"]
    info = {"files": {}, "points": [], "ionic_step": None, "terminal": False, "note": ""}
    if node is None:
        if requested != "info":
            raise AgentError("step_not_started", "该步尚未创建计算任务。", 404)
        return info
    info.update(pk=node.pk, terminal=node.is_terminated, state=node.process_state.value)
    inputs = node.base.repository
    retrieved = node.outputs.retrieved.base.repository if "retrieved" in node.outputs else None
    names = set(retrieved.list_object_names()) if retrieved else set()
    files = info["files"]
    if "POSCAR" in inputs.list_object_names():
        files["POSCAR"] = "实际提交"
    for name in ("CONTCAR", "XDATCAR"):
        if name in names:
            files[name] = "已取回"
    if "XDATCAR" not in files and "vasprun.xml" in names:
        files["XDATCAR"] = "由 vasprun.xml 重建"
    if requested == "POSCAR" and requested in files:
        return {"name": requested, "content": archive_text(inputs, requested)}
    if requested in {"CONTCAR", "XDATCAR"} and requested in names:
        return {"name": requested, "content": archive_text(retrieved, requested)}
    if requested == "XDATCAR" and requested in files:
        return {"name": "trajectory.extxyz", "content": xml_trajectory(retrieved)}
    if requested == "info":
        log = next((name for name in ("OSZICAR", "vasp_output") if name in names), None)
        if log:
            info["points"] = ionic_history(archive_text(retrieved, log, LOG_LIMIT, tail=True))
        if "misc" in node.outputs:
            index = node.outputs.misc.get_dict().get("run_status", {}).get("last_iteration_index")
            if index:
                info["ionic_step"] = index[0]
    if (not node.is_terminated or not retrieved) and requested != "POSCAR":
        _read_live(node, requested, info)
        if "content" in info:
            return {"name": requested, "content": info["content"]}
    if requested != "info":
        raise AgentError("step_file_missing", "该步尚未写出可读的结构文件。", 404)
    if info["points"]:
        info["ionic_step"] = info["points"][-1][0]
    return info


def _read_live(node: orm.CalcJobNode, requested: str, info: dict) -> None:
    if requested not in {"info", "CONTCAR", "XDATCAR"}:
        return
    try:
        with remote_reader(node) as read:
            if not read:
                return
            if requested == "info":
                command = (
                    'for f in CONTCAR XDATCAR; do if [ ! -L "$f" ] && [ -s "$f" ]; '
                    'then echo "$f"; fi; done; echo END; '
                    f"if [ ! -L OSZICAR ] && [ -f OSZICAR ]; then tail -c {LOG_LIMIT} OSZICAR; fi"
                )
                names, _, log = read(command).partition("END\n")
                for name in names.splitlines():
                    if name in {"CONTCAR", "XDATCAR"}:
                        info["files"][name] = "作业目录快照"
                if log:
                    info["points"] = ionic_history(log)
            else:
                data = read(
                    f"test ! -L {requested} && test -s {requested} "
                    f"&& head -c {FILE_LIMIT + 1} {requested}"
                )
                if len(data.encode()) > FILE_LIMIT:
                    raise AgentError("step_file_large", "文件超过在线查看上限（32 MiB）。", 413)
                if requested == "XDATCAR":
                    data = complete_xdatcar(data, len(node.inputs.structure.sites))
                info["content"] = data
    except Exception as exc:
        if requested != "info":
            if isinstance(exc, AgentError):
                raise
            raise AgentError("step_remote_read", "远端快照暂不可用，请刷新重试。", 503) from exc
        info["note"] = "远端快照暂不可用，可稍后刷新。"
