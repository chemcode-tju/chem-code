"""Local structure validation; retired engine requests cannot execute jobs."""

import asyncio
from typing import Any

from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError


class AutomationService:
    """Keep artifact validation independent of any workflow-engine installation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def call(self, action: str, payload: dict | None = None) -> dict[str, Any]:
        if action in {"validate_structure", "validate_structure_file"}:
            from chemistry_agent import structure_inputs

            return await asyncio.to_thread(getattr(structure_inputs, action), payload or {})
        raise AgentError(
            "legacy_backend_removed",
            "旧执行后端已移除；历史记录仍保留。新计算请使用当前 SSH/Slurm 平台。",
            410,
        )
