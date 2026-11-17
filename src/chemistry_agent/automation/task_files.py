"""Validate file references within a platform-managed calculation directory."""

from pathlib import PurePosixPath

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
