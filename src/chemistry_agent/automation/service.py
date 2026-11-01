import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError

logger = logging.getLogger(__name__)


class AutomationService:
    """Isolate AiiDA's global profile and blocking ORM/SSH calls from the web process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = asyncio.Lock()

    async def call(self, action: str, payload: dict | None = None) -> dict[str, Any]:
        if not self.settings.automation_enabled:
            raise AgentError("automation_disabled", "请先配置并启用 AiiDA 自动化。", 503)
        async with self.lock:
            environment = os.environ.copy()
            environment["PATH"] = (
                f"{Path(sys.executable).parent}{os.pathsep}{environment.get('PATH', '')}"
            )
            environment["AIIDA_PATH"] = str(self.settings.aiida_path)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "chemistry_agent.automation.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            message = json.dumps(
                {
                    "action": action,
                    "payload": payload or {},
                    "profile": self.settings.aiida_profile,
                }
            ).encode()
            try:
                timeout = 360 if action in {"preview", "branch_retry"} else 90
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(message), timeout=timeout
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AgentError(
                    "automation_timeout",
                    "操作超时；若正在提交，请用同一预览重试查询，勿新建重复任务。",
                    504,
                ) from exc
            if process.returncode:
                logger.error(
                    "Automation worker failed: %s", stderr.decode(errors="replace")[-4000:]
                )
                raise AgentError(
                    "automation_worker_failed", "AiiDA 工作进程异常，请检查后台日志。", 503
                )
            try:
                result = json.loads(stdout)
            except ValueError as exc:
                raise AgentError(
                    "automation_invalid_response", "AiiDA 返回格式异常。", 502
                ) from exc
            if "error" in result:
                logger.warning("Automation error: %s", stderr.decode(errors="replace")[-4000:])
                raise AgentError(**result["error"])
            return result
