"""Run a small execution relay on a login/management host that allows services."""

import os
import secrets
from pathlib import Path

import uvicorn

from chemistry_agent.environments.runner import create_runner

# Configuration
state_directory = Path.home() / ".local" / "state" / "chem-runner"
listen_address = "127.0.0.1"
port = 8767


def main() -> None:
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path = state_directory / "connection-token"
    try:
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    token_path.chmod(0o600)
    app = create_runner(state_directory, token_path.read_text().strip())
    print(f"Runner token file: {token_path}")
    uvicorn.run(app, host=listen_address, port=port)


if __name__ == "__main__":
    main()
