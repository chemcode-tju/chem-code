"""Single writer lease for a persisted executor/inbox runtime."""

from filelock import FileLock, Timeout

from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError


class RuntimeLease:
    def __init__(self, settings: Settings) -> None:
        settings.aiida_path.mkdir(parents=True, exist_ok=True)
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.locks = [
            FileLock(path)
            for path in sorted(
                {
                    settings.aiida_path / ".computation-platform.lock",
                    settings.database_path.with_suffix(".platform.lock"),
                }
            )
        ]

    def __enter__(self):
        try:
            for lock in self.locks:
                lock.acquire(timeout=0)
        except Timeout as exc:
            for lock in self.locks:
                lock.release()
            raise AgentError(
                "platform_already_running",
                "此数据目录已有平台后台运行，请勿同时启动两个执行器。",
                409,
            ) from exc
        return self

    def __exit__(self, *args) -> None:
        for lock in self.locks:
            lock.release()
