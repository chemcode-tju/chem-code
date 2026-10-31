"""Start the local automation workspace after setup_automation.py has completed."""
# ruff: noqa: E402
# Configuration
port = 8766

import uvicorn

from chemistry_agent.api import create_app
from chemistry_agent.config import Settings


def main() -> None:
    settings = Settings(automation_enabled=True, host="127.0.0.1", port=port)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
