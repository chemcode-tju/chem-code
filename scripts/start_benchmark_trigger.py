"""One authorized completion trigger. No model is invoked while the job runs."""
# ruff: noqa: E402

# Configuration
deployment_directory = "/data/home/researcher/.local/share/chem-research-plugins"
control_socket = "/data/home/researcher/.codex/app-server-control/app-server-control.sock"
workspace_id = "ee4bacc1-f91b-5132-a392-f6b41bece61d"
wait_id = "61a962cf-06c2-57ff-ae5b-641a311ced8c"
thread_id = "01a1f2ab-c87f-72e2-ac3a-70afe22b1da2"
native_job_id = "e43167dc-77c0-5b20-8d98-5ef215903462"
native_scheduler_id = "1755"
native_directory = "/data/home/researcher/work/Cu-Cation/mace_lammps_benchmark/cpu80/native_1755"
ase_job_id = "d4bd40ea-9295-501a-9555-22635eb041af"
ase_scheduler_id = "1756"
ase_directory = "/data/home/researcher/work/Cu-Cation/mace_lammps_benchmark/cpu80/ase_1756"
poll_seconds = 10

import logging
import sys
from pathlib import Path

from filelock import FileLock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from chemistry_agent.research_plugins.app_server_client import AppServerClient
from chemistry_agent.research_plugins.trigger import OneShotTrigger, TriggerJob


def main() -> None:
    state = Path(deployment_directory) / "state"
    state.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(state / "trigger-benchmark.log"), logging.StreamHandler()],
    )
    with FileLock(str(state / f"trigger-{wait_id}.lock"), timeout=0):
        trigger = OneShotTrigger(
            state / "monitor.sqlite3",
            state / "trigger-deliveries.sqlite3",
            workspace_id,
            wait_id,
            thread_id,
            (
                TriggerJob(
                    native_job_id,
                    native_scheduler_id,
                    Path(native_directory),
                    "LAMMPS 原生 pair_style mace，80 核",
                ),
                TriggerJob(
                    ase_job_id,
                    ase_scheduler_id,
                    Path(ase_directory),
                    "ASE VelocityVerlet 直接积分，80 核；不是 Python 回调",
                ),
            ),
        )
        trigger.run(lambda: AppServerClient(Path(control_socket)), poll_seconds)


if __name__ == "__main__":
    main()
