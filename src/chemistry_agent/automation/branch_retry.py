"""Preview replacement of failed adsorption branches, retaining immutable successful outputs."""

import hashlib
import json
from uuid import UUID, uuid4

from aiida import orm

from chemistry_agent.automation import backend
from chemistry_agent.automation.adsorption import valid_branch
from chemistry_agent.automation.constraints import structure_text
from chemistry_agent.automation.recovery import RecoveryDecision, RecoveryPolicy, apply_recovery
from chemistry_agent.automation.surface import restore_dynamics, surface_builder
from chemistry_agent.errors import AgentError


def preview_retry(payload: dict) -> dict:
    root = backend.automation_directory()
    original = str(UUID(payload["preview_id"]))
    record = json.loads((root / original / "preview.json").read_text())
    job = orm.load_node(uuid=str(UUID(payload["job_id"])))
    if job.label != backend.PREFIX + original or not job.is_terminated or job.exit_status != 434:
        raise AgentError(
            "branch_retry_unavailable", "仅能自动重试已结束且未通过检查的吸附并行阶段。", 422
        )
    reuse = {key: job.outputs[key].uuid for key in ("structure", "results", "summary", "dynamics")}
    requests = record["stage_requests"]
    stages = record["stages"]
    files = {}
    children = {
        child.label.split(":")[-1]: child
        for child in job.called
        if isinstance(child, orm.CalcJobNode)
    }
    retry_count = 0
    for step in stages:
        key = step["stage"]
        if key in ("surface", "bulk"):
            if key in children:
                step["reused_pk"] = children[key].pk
            continue
        if not step.get("parallel"):
            continue
        if key in record.get("reuse_nodes", {}):
            reuse[key] = record["reuse_nodes"][key]
            continue
        node = children[key]
        if valid_branch(node):
            reuse[key] = node.outputs.misc.uuid
            step["reused_pk"] = node.pk
            continue
        if not node.is_terminated:
            raise AgentError("branch_running", "子任务还未结束。", 409)
        updated = apply_recovery(
            {
                "request": requests[key],
                "recovery_baseline_resources": payload["baseline"],
                "recovery_attempt": payload["attempt"],
            },
            RecoveryDecision.model_validate(payload["decision"]),
            RecoveryPolicy.model_validate(payload["policy"]),
        )
        atoms = node.inputs.structure.get_ase()
        if "dynamics" in node.inputs:
            restore_dynamics(atoms, node.inputs.dynamics.get_dict()["positions_dof"])
        updated["structure"] = {"format": "extxyz", "content": structure_text(atoms)}
        dry = backend.preview(updated)
        files.update({f"{key}/{name}": content for name, content in dry["files"].items()})
        requests[key] = updated
        step.update(incar=updated["incar"], resources=updated["resources"])
        retry_count += 1
    if not retry_count:
        raise AgentError("no_failed_branch", "没有需要重新计算的位点。", 409)
    identifier = str(uuid4())
    record.update(
        preview_id=identifier,
        created_at=backend.now(),
        files=files,
        reuse_nodes=reuse,
        max_calculations=retry_count,
        total_wallclock_limit_seconds=sum(
            requests[s["stage"]]["resources"]["wallclock_seconds"]
            for s in stages
            if s.get("parallel") and s["stage"] not in reuse
        ),
    )
    _, record["execution_fingerprint"] = surface_builder(record, backend.PREFIX + identifier)
    record["input_sha256"] = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    directory = root / identifier
    directory.mkdir(mode=0o700)
    (directory / "preview.json").write_text(json.dumps(record, ensure_ascii=False))
    return {
        k: v for k, v in record.items() if k not in {"stage_requests", "request", "reuse_nodes"}
    }
