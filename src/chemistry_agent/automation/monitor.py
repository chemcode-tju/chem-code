"""Background monitoring is independent of browser tabs and model availability."""

import asyncio
import contextlib
import json
import logging
from uuid import UUID

from chemistry_agent.automation.events import EventStore, utc_now
from chemistry_agent.automation.recovery import RecoveryAgent

logger = logging.getLogger(__name__)


class TaskMonitor:
    def __init__(self, chat, provider, interval: float = 20, *, session_store=None):
        self.chat = chat
        self.events = EventStore(chat.service.settings.aiida_path / "task-monitor.sqlite3")
        self.recovery = RecoveryAgent(chat, self.events, provider, session_store=session_store)
        self.interval = interval
        self.tasks: list[asyncio.Task] = []
        self.lock = asyncio.Lock()

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self.watch()), asyncio.create_task(self.recover())]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def tracked(self) -> dict:
        records = {}
        for path in self.chat.root.glob("*/submissions/*.json"):
            try:
                session = str(UUID(path.parent.parent.name))
                preview_id = str(UUID(path.stem))
                item = json.loads(path.read_text())
                identifier = str(UUID(item["job"]["id"]))
                records[identifier] = (session, preview_id)
            except (ValueError, KeyError, OSError):
                logger.warning("Skipping invalid submission record")
        return records

    async def tick(self) -> None:
        async with self.lock:
            tracked = self.tracked()
            initial = not self.events.initialized()
            identifiers = list(tracked)
            for start in range(0, len(identifiers), 100):
                response = await self.chat.service.call(
                    "tracked_jobs", {"job_ids": identifiers[start : start + 100]}
                )
                for job in response["jobs"]:
                    if job["id"] not in tracked:
                        continue
                    session, preview = tracked[job["id"]]
                    root = self.chat.load(session, "previews", preview).get(
                        "recovery_root_id", job["id"]
                    )
                    self.events.observe(
                        session,
                        preview,
                        job,
                        diagnose=not initial and self.events.policy(root)["mode"] != "off",
                    )
            self.events.set_meta("initialized", "true")
            self.events.set_meta("last_success", utc_now())
            self.events.set_meta("error", "")

    async def watch(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.events.set_meta(
                    "error", f"状态同步失败（{type(exc).__name__}），展示的是最后成功记录。"
                )
                logger.warning("Task monitor failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.interval)

    async def recover(self) -> None:
        while True:
            try:
                item = self.events.next_recovery()
                if item:
                    await self.recovery.process(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Recovery worker failed (%s)", type(exc).__name__)
            await asyncio.sleep(5)
