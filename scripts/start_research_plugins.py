"""Start the independent research daemon; edit only Configuration below."""
# ruff: noqa: E402

# Configuration
data_directory = "data/research-plugins"
listen_port = 29841
ssh_hosts = ""
project_directory = "."

import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chemistry_agent.research_plugins.daemon import create_app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    home = (root / data_directory).resolve()
    app = create_app(
        home,
        tuple(host.strip() for host in ssh_hosts.split(",") if host.strip()),
        ((root / project_directory).resolve(),),
    )
    uvicorn.run(app, host="127.0.0.1", port=listen_port)


if __name__ == "__main__":
    main()
