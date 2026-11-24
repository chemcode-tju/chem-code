"""Small event notifications; authoritative plans and evidence remain available on demand."""

import json

WAKEUP_INSTRUCTIONS = (
    "后台任务结束事件 / 研究步骤就绪事件（平台通知，非用户新消息）。"
    "继续已授权目标；step_ready 由你执行，其他事件先核验真实输出。"
    "操作前用 calculation_plan_get(plan_id,node_id) 读取相关步骤的最新说明、参数、"
    "提交脚本和已登记作业，不能用旧记录覆盖用户修改。完整方案可按需 full=true；"
    "原始输出用 calculation_job_files(job_id)，其他工具照常可用。"
    "已接单作业不重复提交，回执不明先对账；退出码0不等于科学收敛。"
    "instruction 中若有 read_from，须通过 event_details 读取完整指令后再行动。"
    "关联的 Agent task 用 calculation_step_report 记录结论；平台 job 的状态由平台维护，"
    "无关联 task 时直接总结。若需等待外部任务，登记持久等待并结束本轮。"
    "本通知只含事件摘要，未附内容不代表不存在；只向用户展示分析与行动。\n"
)


def instruction_reference(instruction: str, path: str) -> str | dict:
    """Keep ordinary follow-up instructions verbatim; link long ones without losing them."""
    if len(instruction) <= 1200:
        return instruction
    return {"preview": instruction[:240], "characters": len(instruction), "read_from": path}


def event_summary(item: dict, plan: dict, index: int, instruction: str) -> dict:
    """Project identifiers and status only, without copying scientific inputs or outputs."""
    node_id = item["node_id"]
    node = next((node for node in plan["nodes"] if node["id"] == node_id), {})
    current = plan["runtime"].get(node_id, {})
    recorded = item.get("state") or {}
    result = {
        "node_id": node_id,
        "type": item.get("type")
        or ("job_finished" if item.get("outcome") in {"completed", "failed"} else "wait_matched"),
        "outcome": item.get("outcome"),
        "attempt": item.get("attempt"),
        "current_status": current.get("status"),
        "title": node.get("title"),
        "kind": node.get("kind"),
        "executor": node.get("executor"),
        "parent_task": item.get("parent_task") or node.get("parent_task"),
        "host": (node.get("job") or {}).get("host") or node.get("execution_host"),
        "job_id": recorded.get("generic_job_id"),
        "scheduler_job_id": recorded.get("scheduler_job_id"),
        "remote_directory": recorded.get("remote_directory"),
    }
    # step_ready purpose is read from the latest plan, not the event's old snapshot.
    follow_up = recorded.get("instruction")
    if follow_up and follow_up != instruction:
        result["instruction"] = instruction_reference(
            follow_up, f"payload.events[{index}].state.instruction"
        )
    if recorded.get("error"):
        error = recorded["error"]
        text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
        result["error"] = text[:240]
        result["error_ref"] = f"payload.events[{index}].state.error"
    return {key: value for key, value in result.items() if value is not None}


def build_wakeup_message(plan: dict, event_id: str, payload: dict) -> str:
    """Build a notification without changing the durable event or execution authority."""
    instruction = payload.get("instruction")
    instruction_path = "payload.instruction"
    if not instruction:
        job_instructions = {
            (item.get("state") or {}).get("instruction") for item in payload["events"]
        }
        if len(job_instructions) == 1 and all(job_instructions):
            instruction = job_instructions.pop()
            instruction_path = "payload.events[0].state.instruction"
        else:
            instruction = "按各步骤的指令检查结果并继续已授权的目标"
    body = {
        "event_id": event_id,
        "event_type": "task_event",
        "plan_id": plan["id"],
        "plan_revision": plan.get("revision"),
        "approved_revision": plan.get("approved_revision"),
        "plan_status": plan["status"],
        "auto_queue": plan.get("auto_queue"),
        "instruction": instruction_reference(instruction, instruction_path),
        "events": [
            event_summary(item, plan, index, instruction)
            for index, item in enumerate(payload["events"])
        ],
        "event_details": {
            "tool": "calculation_wait_control",
            "arguments": {"plan_id": plan["id"], "action": "list", "event_id": event_id},
        },
    }
    if payload.get("wait_id"):
        body["wait_id"] = payload["wait_id"]
    if payload.get("file_observations"):
        body["file_observations"] = [
            {
                "node_id": node_id,
                "attempt": observation.get("attempt"),
                "file_count": len(observation.get("files", [])),
                "read_from": f"payload.file_observations.{node_id}",
            }
            for node_id, observation in payload["file_observations"].items()
        ]
    return WAKEUP_INSTRUCTIONS + json.dumps(body, ensure_ascii=False, separators=(",", ":"))
