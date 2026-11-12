"""Versioned edits to steps which have not started; never rewrite submitted jobs."""

import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.plan_models import PlanNode
from chemistry_agent.errors import AgentError
from chemistry_agent.store import utc_now


class StepEdit(StrictModel):
    expected_revision: int = Field(ge=1)
    field: Literal["purpose", "INCAR", "KPOINTS", "script"]
    content: str = Field(max_length=200000)


def edit_policy(plan: dict, node: dict) -> dict:
    state = plan["runtime"][node["id"]]
    started = (
        state.get("status") not in {"pending", "blocked", "ready", "waiting"}
        or any(state.get(key) for key in ("started_at", "job_id", "generic_job_id", "jobs"))
        or (node.get("job") or {}).get("adopted")
        or plan["status"] in {"completed", "failed", "stopping", "stopped"}
    )
    # A goal with an executing child is also already underway.
    started = started or any(
        child.get("parent_task") == node["id"]
        and (
            plan["runtime"][child["id"]].get("status") != "pending"
            or plan["runtime"][child["id"]].get("generic_job_id")
        )
        for child in plan["nodes"]
    )
    return {
        "revision": plan["revision"],
        "purpose": not started,
        "files": not started and node["kind"] in {"job", "task"},
        "reason": "步骤已开始或作业已提交，输入文件与说明已锁定。" if started else "",
    }


def planned_text(node: dict) -> dict:
    settings = node.get("job") or {}
    files = settings.get("files", {}) if node["kind"] == "job" else node.get("text_files", {})
    return {
        "parameters": {name: files.get(name, "") for name in ("INCAR", "KPOINTS")},
        "script": settings.get("script", "")
        if node["kind"] == "job"
        else node.get("submission_script", ""),
    }


async def edit_step(plans, session: str, identifier: str, node_id: str, change: StepEdit) -> dict:
    """Compare revision and execution state inside the same lock used by submission."""
    async with plans.lock(session, identifier):
        plan = plans.chat.load(session, "plans", identifier)
        node = next((item for item in plan["nodes"] if item["id"] == node_id), None)
        if node is None:
            raise AgentError("step_not_found", "此方案没有该步骤。", 404)
        if plan["revision"] != change.expected_revision:
            raise AgentError(
                "plan_revision_conflict", "方案已有更新，请刷新核对后保存；编辑内容已保留。", 409
            )
        policy = edit_policy(plan, node)
        if not policy["purpose" if change.field == "purpose" else "files"]:
            raise AgentError(
                "step_edit_locked", policy["reason"] or "此步骤不支持直接编辑参数文件。", 409
            )
        if plans.store:
            async with plans.store.lock:
                cursor = await plans.store.db.execute(
                    "SELECT payload FROM agent_events WHERE session_id=? AND plan_id=? "
                    "AND status='running'",
                    (session, identifier),
                )
                claimed = await cursor.fetchall()
            if any(
                event.get("node_id") == node_id and event.get("type") == "step_ready"
                for row in claimed
                for event in json.loads(row["payload"]).get("events", [])
            ):
                raise AgentError(
                    "step_edit_locked", "Agent 已开始准备此步骤，请在对话中提出修改。", 409
                )
        if "\x00" in change.content:
            raise AgentError("invalid_step_text", "文本不能包含空字符。", 422)
        # Keep the prior revision intact, including its approved scientific settings.
        import copy

        previous = copy.deepcopy(plan)
        if change.field == "purpose":
            if len(change.content) > 8000:
                raise AgentError("step_description_long", "步骤说明最多 8000 字符。", 422)
            node["purpose"] = change.content
        elif change.field == "script":
            if node["kind"] == "job":
                node["job"]["script"] = change.content
            else:
                node["submission_script"] = change.content
        else:
            files = (
                node["job"].setdefault("files", {})
                if node["kind"] == "job"
                else node.setdefault("text_files", {})
            )
            files[change.field] = change.content
        # Do not silently accept a save that violates the executable node schema.
        try:
            PlanNode.model_validate(
                {key: value for key, value in node.items() if key in PlanNode.model_fields}
            )
        except ValidationError as exc:
            raise AgentError("invalid_step_text", str(exc.errors()[0]["msg"]), 422) from exc
        plan["revision"] += 1
        plan["changed_nodes"] = [node_id]
        plan.setdefault("history", []).append(
            {
                "revision": plan["revision"],
                "changed_nodes": [node_id],
                "removed_nodes": [],
                "source": "user_step_edit",
                "field": change.field,
                "at": utc_now(),
            }
        )
        plans.chat.save(session, "plan_versions", str(uuid4()), previous)
        plans.save(session, plan)
        return plans.public(plan)
