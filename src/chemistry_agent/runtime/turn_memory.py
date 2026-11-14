"""Fit a long active tool loop without splitting function calls from their results."""

import json

from chemistry_agent.tokens import TokenCounter


def fold_tool_rounds(
    current: list[dict], counter: TokenCounter, budget: int
) -> tuple[list[dict], int]:
    """Retain recent reasoning context and prioritize receipts over repeated reads."""
    rounds = [
        i
        for i, msg in enumerate(current)
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    if len(rounds) <= 3:
        return current, 0
    boundary = rounds[-3]
    folded = current[rounds[0] : boundary]
    names = {c["id"]: c["function"]["name"] for msg in folded for c in msg.get("tool_calls", [])}
    rows = []
    receipts = {
        "calculation_step_report",
        "calculation_job_adopt",
        "calculation_submit_confirmed",
        "calculation_wait_register",
        "calculation_artifact_register",
        "calculation_plan_save",
        "calculation_plan_extend",
        "calculation_plan_repair",
        "memory_save",
    }
    for msg in folded:
        if msg["role"] == "tool":
            try:
                result = json.loads(msg["content"])
            except ValueError:
                result = {}
            keys = (
                "plan_id",
                "job_id",
                "node_id",
                "wait_id",
                "revision",
                "id",
                "structure_id",
                "analysis_id",
                "preview_id",
                "scheduler_job_id",
                "pk",
                "status",
                "error",
                "formula",
                "exit_code",
                "message",
                "accepted_jobs",
                "artifact_ids",
                "submitted",
                "monitoring",
            )
            refs = {k: result[k] for k in keys if isinstance(result, dict) and k in result}
            excerpt = counter.prefix(
                json.dumps(refs or result, ensure_ascii=False), min(384, budget // 2)
            )
            name = names.get(msg.get("tool_call_id"), "tool")
            rows.append((name in receipts, f"{name}: {excerpt}"))
    # Prefer the most recent observations. Never present a truncated catalog as exhaustive.
    lines = []
    # Successful actions must not disappear beneath hundreds of plan/status reads.
    # This is a bounded source excerpt, not a replacement for original evidence.
    unique = list(dict.fromkeys(reversed(rows)))
    for _, line in sorted(unique, key=lambda item: not item[0]):
        if counter.text_tokens("\n".join([line, *lines])) > budget:
            continue
        lines.insert(0, line)
    memory = {
        "role": "assistant",
        "content": (
            f"[本轮前 {len(rounds) - 3} 组工具结果摘录；优先保留动作回执。"
            "不要重复已产生副作用的操作，"
            "需要精确参数或状态时查询对应记录。]\n" + "\n".join(lines)
        ),
    }
    return [*current[: rounds[0]], memory, *current[boundary:]], len(rows)
