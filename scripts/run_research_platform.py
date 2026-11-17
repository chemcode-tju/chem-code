"""Run the web UI, persistent monitor and replaceable Agent on this machine."""

from pathlib import Path

import uvicorn

from chemistry_agent.api import create_app
from chemistry_agent.config import Settings

# Configuration
data_directory = Path.home() / ".local" / "share" / "chem-code"
listen_address = "127.0.0.1"
port = 8766


def main() -> None:
    settings = Settings(
        _env_file=None,
        database_path=data_directory / "agent.sqlite3",
        platform_path=data_directory / "platform",
        tokenizer_path=data_directory / "deepseek-tokenizer.json",
        automation_enabled=True,
    )
    uvicorn.run(create_app(settings), host=listen_address, port=port)


if __name__ == "__main__":
    main()
