"""Compact display hints; these never select a scientific workflow."""

from pathlib import Path

from chemistry_agent.automation.result_reader import result_file


def display_source(hint: str) -> dict:
    """Expand a one-line hint locally instead of asking a model to repeat metadata."""
    if hint == "auto":
        return {"adapter": "auto"}
    if hint == "neb" or hint.startswith("neb:"):
        pattern = hint[4:] if hint.startswith("neb:") else "paths/step_*/path.json"
        if not pattern or "**" in pattern:
            raise ValueError("NEB 展示请使用明确的快照模式。")
        result_file(Path("/registered-job"), pattern)
        return {"adapter": "json_paths", "pattern": pattern}
    if not hint.strip():
        raise ValueError("结果索引路径不能为空。")
    result_file(Path("/registered-job"), hint)
    return {"adapter": "manifest", "path": hint}
