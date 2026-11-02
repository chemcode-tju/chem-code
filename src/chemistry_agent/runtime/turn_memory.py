"""Fit a long active tool loop without splitting function calls from their results."""

import json

from chemistry_agent.tokens import TokenCounter


def fold_tool_rounds(
    current: list[dict], counter: TokenCounter, budget: int
) -> tuple[list[dict], int]:
    """Keep the original user input and latest complete tool round verbatim."""
    rounds = [
        i
        for i, msg in enumerate(current)
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    if len(rounds) < 2:
        return current, 0
    folded = current[rounds[0] : rounds[-1]]
    names = {c["id"]: c["function"]["name"] for msg in folded for c in msg.get("tool_calls", [])}
    rows = []
    for msg in folded:
        if msg["role"] == "tool":
            try:
                result = json.loads(msg["content"])
            except ValueError:
                result = {}
            keys = (
                "plan_id",
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
            )
            refs = {k: result[k] for k in keys if isinstance(result, dict) and k in result}
            excerpt = counter.prefix(
                json.dumps(refs or result, ensure_ascii=False), min(384, budget // 2)
            )
            rows.append(f"{names.get(msg.get('tool_call_id'), 'tool')}: {excerpt}")
    # Prefer the most recent observations. Never present a truncated catalog as exhaustive.
    lines = []
    for line in reversed(rows):
        if counter.text_tokens("\n".join([line, *lines])) > budget:
            break
        lines.insert(0, line)
    memory = {
        "role": "assistant",
        "content": (
            f"[本轮前 {len(rounds) - 1} 组工具结果摘录；不要重复已产生副作用的操作，"
            "需要精确参数或状态时查询对应记录。]\n" + "\n".join(lines)
        ),
    }
    return [*current[: rounds[0]], memory, *current[rounds[-1] :]], len(rows)
