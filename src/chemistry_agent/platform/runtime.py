"""Run persisted computation/monitoring without constructing an LLM client or agent."""

from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.config import Settings
from chemistry_agent.platform.lease import RuntimeLease
from chemistry_agent.store import Store


class PlatformRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = None
        self.lease = RuntimeLease(settings)
        self.chat = CalculationChat(AutomationService(settings))
        self.platform = self.chat.plans.platform

    async def __aenter__(self):
        self.lease.__enter__()
        try:
            self.store = await Store.open(self.settings.database_path)
            self.store.research_memory_enabled = False
            self.chat.attachment_store = self.store
            self.chat.plans.start(self.store)
        except BaseException:
            if self.store:
                await self.store.close()
            self.lease.__exit__()
            raise
        return self

    async def __aexit__(self, *args) -> None:
        try:
            await self.chat.plans.close()
            await self.store.close()
        finally:
            self.lease.__exit__()
