"""Standalone monitoring/execution host; attach agent adapters separately if desired."""

import asyncio
from pathlib import Path

from chemistry_agent.config import Settings
from chemistry_agent.platform.runtime import PlatformRuntime

# Configuration
workspace = Path(__file__).resolve().parents[1]
database_path = workspace / "data" / "agent.sqlite3"
runtime_directory = workspace / "data" / "aiida"


async def main() -> None:
    settings = Settings(database_path=database_path, platform_path=runtime_directory)
    async with PlatformRuntime(settings):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
