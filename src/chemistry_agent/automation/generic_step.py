"""Read VASP artifacts from ordinary monitored jobs without invoking AiiDA."""

import asyncio
import math
import re
from pathlib import Path

from chemistry_agent.automation.slurm_remote import vasp_snapshot
from chemistry_agent.automation.step_edit import edit_policy, planned_text
from chemistry_agent.errors import AgentError


def ionic_points(text: str) -> list[list]:
    points = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"\s*(\d+)\s+F=\s*\S+\s+E0=\s*([-+.\dEeDd]+)", line)
        if match and line.endswith(("\n", "\r")):
            try:
                value = float(match[2].replace("D", "E").replace("d", "e"))
            except ValueError:
                continue
            if math.isfinite(value):
                points.append([int(match[1]), value])
    return points


def complete_frames(text: str) -> str:
    """Ignore an incomplete final XDATCAR frame instead of passing it to the viewer."""
    header = text.splitlines()[:7]
    if len(header) < 7:
        raise AgentError("step_no_frames", "轨迹头部尚未写完。", 409)
    counts = header[5].split() if all(v.isdigit() for v in header[5].split()) else header[6].split()
    count = sum(int(value) for value in counts)
    markers = list(re.finditer(r"(?im)^\s*(?:Direct|Cartesian) configuration\s*=.*?\n", text))
    for marker in reversed(markers):
        lines = text[marker.end() :].splitlines(keepends=True)
        if len(lines) >= count and all(line.endswith(("\n", "\r")) for line in lines[:count]):
            return text[: marker.end()] + "".join(lines[:count])
    raise AgentError("step_no_frames", "尚无完整的轨迹帧。", 409)


async def inspect_generic_step(
    chat, session: str, plan: dict, node: dict, requested: str, selected: str | None
) -> dict:
    state = plan["runtime"][node["id"]]
    identifiers = list(
        dict.fromkeys(
            ([state["generic_job_id"]] if state.get("generic_job_id") else [])
            + state.get("jobs", [])
        )
    )
    records = [chat.plans.slurm.load(session, identifier) for identifier in identifiers]
    if selected and selected not in identifiers:
        raise AgentError("step_job_mismatch", "此作业不属于当前步骤。", 404)
    accepted = state.get("accepted_jobs", [])
    identifier = selected or (
        accepted[-1] if accepted else identifiers[-1] if identifiers else None
    )
    record = next((item for item in records if item["id"] == identifier), None)
    info = {
        "files": {},
        "points": [],
        "ionic_step": None,
        "terminal": False,
        "jobs": [
            {
                "id": item["id"],
                "scheduler_job_id": item.get("scheduler_job_id"),
                "state": item.get("scheduler_state", item.get("status")),
            }
            for item in records
        ],
        "selected_job_id": identifier,
        "note": "",
        "parameters": {},
        "editing": edit_policy(plan, node),
    }
    if record is None:
        if requested != "info":
            raise AgentError("step_not_started", "此步骤尚未关联计算作业。", 404)
        info.update(planned_text(node))
        return info
    info.update(
        terminal=bool(record.get("terminal")), scheduler_job_id=record.get("scheduler_job_id")
    )
    folder = record.get("retrieved_directory")
    local = await asyncio.to_thread(vasp_snapshot, Path(folder), requested) if folder else None
    snapshot = local or {"files": {}, "contents": {}}
    sources = dict.fromkeys(snapshot["files"], "已取回")
    # Running jobs must use live files. Completed jobs fall back to remote files
    # if the retrieval policy did not include the requested scientific artifacts.
    missing = (
        requested not in snapshot["contents"]
        if requested != "info"
        else not all(
            name in snapshot["files"]
            for name in ("POSCAR", "CONTCAR", "XDATCAR", "INCAR", "KPOINTS", "OSZICAR")
        )
    )
    if not record.get("terminal") or missing:
        try:
            remote = await chat.plans.slurm.remote(record, "vasp_snapshot", file=requested)
            first, last = (remote, snapshot) if record.get("terminal") else (snapshot, remote)
            snapshot = {
                "files": {**first["files"], **last["files"]},
                "contents": {**first["contents"], **last["contents"]},
            }
            remote_sources = dict.fromkeys(remote["files"], "作业目录快照")
            sources = (
                {**remote_sources, **sources}
                if record.get("terminal")
                else {**sources, **remote_sources}
            )
        except AgentError:
            if not local:
                raise
            info["note"] = "远端暂不可用，显示已取回文件。"
    info["files"] = {
        name: sources[name]
        for name in ("POSCAR", "CONTCAR", "XDATCAR")
        if name in snapshot["files"]
    }
    contents = snapshot["contents"]
    if requested != "info":
        text = contents.get(requested)
        if not text:
            raise AgentError("step_file_missing", "此作业尚未生成该结构文件。", 404)
        if requested == "XDATCAR":
            text = complete_frames(text)
        return {"name": requested, "content": text}
    info["parameters"] = {name: contents[name] for name in ("INCAR", "KPOINTS") if name in contents}
    info["script"] = next(
        (
            contents[name]
            for name in ("submit.sh", "run.slurm", "job.sh", "_chem_submit.sh")
            if name in contents
        ),
        "",
    )
    info["points"] = ionic_points(contents.get("OSZICAR", ""))
    info["ionic_step"] = info["points"][-1][0] if info["points"] else None
    return info
