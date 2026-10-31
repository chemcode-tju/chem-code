# ruff: noqa: E402
"""Create a dedicated local AiiDA profile and start its daemon; never submit jobs."""

# Configuration
profile_name = "chem-agent"
user_email = "local@chem-agent.invalid"

import os
import subprocess
import sys
from pathlib import Path

from chemistry_agent.config import Settings


def main() -> None:
    settings = Settings()
    os.environ["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["AIIDA_PATH"] = str(settings.aiida_path)
    from aiida.manage.configuration import get_config

    verdi = str(Path(sys.executable).with_name("verdi"))
    config = get_config(create=True)
    if profile_name not in config.profile_names:
        subprocess.run(
            [
                verdi,
                "presto",
                "--non-interactive",
                "--use-zeromq",
                "--profile-name",
                profile_name,
                "--email",
                user_email,
            ],
            check=True,
        )
    else:
        print(f"Keeping existing profile: {profile_name}")
    from aiida import load_profile
    from aiida.engine.daemon.client import get_daemon_client
    from aiida.manage.configuration import reset_config

    reset_config()
    load_profile(profile_name)
    if not get_daemon_client().is_daemon_running:
        subprocess.run([verdi, "-p", profile_name, "daemon", "start", "1"], check=True)
    print(f"Ready. AIIDA_PATH={settings.aiida_path}; profile={profile_name}")


if __name__ == "__main__":
    main()
