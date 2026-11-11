"""Install the local calculation workspace as a macOS user LaunchAgent."""

import os
import plistlib
import socket
import subprocess
import sys
from pathlib import Path

# Configuration
project_directory = Path(__file__).resolve().parents[1]


def install_service(project: Path) -> Path:
    """Keep the existing local automation settings and restart after process exit."""
    if sys.platform != "darwin":
        raise RuntimeError("This installer requires macOS launchd.")
    project = project.resolve()
    interpreter = project / ".conda/bin/python"
    startup = project / "scripts/start_automation.py"
    if not interpreter.is_file() or not startup.is_file():
        raise FileNotFoundError(f"Missing automation environment or startup script in {project}")
    label = "com.chem-code.automation"
    destination = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    log_directory = project / "data"
    config = {
        "Label": label,
        "ProgramArguments": [str(interpreter), str(startup)],
        "WorkingDirectory": str(project),
        "EnvironmentVariables": {"PYTHONPATH": str(project / "src"), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "StandardOutPath": str(log_directory / "automation-service.log"),
        "StandardErrorPath": str(log_directory / "automation-service-error.log"),
    }
    if destination.exists() and plistlib.loads(destination.read_bytes()) != config:
        raise RuntimeError(f"An existing service has different settings: {destination}")
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True)
    if loaded.returncode:
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex(("127.0.0.1", 8766)) == 0:
                raise RuntimeError("Port 8766 is already in use. Stop the existing service first.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_directory.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plistlib.dumps(config))
    destination.chmod(0o600)
    if loaded.returncode:
        subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    print(f"Automation service installed: {destination}")
    return destination


if __name__ == "__main__":
    install_service(project_directory)
