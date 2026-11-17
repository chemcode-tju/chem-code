"""Detached stdlib observer. No Agent, scheduler submission or scientific decisions."""

import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Configuration
poll_seconds = 30


def write(path: Path, data: dict) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o600)
    temporary.replace(path)


def observe(directory: Path, watch: dict) -> dict:
    request = watch["request"]
    if request["action"] not in {"status", "observe_external"}:
        raise ValueError("Monitor accepts observation only")
    command = [sys.executable, str(directory / "slurm-reader.py")]
    if watch.get("target"):
        command = [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=8",
            "-oClearAllForwardings=yes",
            "--",
            watch["target"],
            "python3 -c " + shlex.quote((directory / "slurm-reader.py").read_text()),
        ]
    result = subprocess.run(
        command, input=json.dumps(request), capture_output=True, text=True, timeout=45, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:] or "Scheduler observation failed")
    data = json.loads(result.stdout)
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def tick(directory: Path) -> None:
    for path in sorted(directory.glob("watch-*.json")):
        with path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            watch = json.loads(path.read_text())
            if not watch["active"] or watch.get("result", {}).get("terminal"):
                continue
            try:
                watch["result"] = observe(directory, watch)
                watch["observed_at"] = time.time()
                watch.pop("error", None)
            except Exception as exc:
                watch["error"] = f"{type(exc).__name__}: {exc}"
            write(path, watch)


def worker(directory: Path) -> None:
    with (directory / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            write(directory / "heartbeat.json", {"pid": os.getpid(), "time": time.time()})
            tick(directory)
            time.sleep(poll_seconds)


def control(request: dict) -> dict:
    directory = Path(request["directory"]).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = request["key"]
    if not identifier.replace("-", "").isalnum():
        raise ValueError("Invalid watch identity")
    path = directory / f"watch-{identifier}.json"
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(path.read_text()) if path.exists() else None
        generation = request["generation"]
        if old and old["generation"] > generation:
            return {"fenced": True, "generation": old["generation"]}
        operation = request["operation"]
        if request.get("reader"):
            write_reader = directory / "slurm-reader.py"
            write_reader.write_text(request["reader"])
            write_reader.chmod(0o600)
        if operation in {"prepare", "enable"}:
            if not old or old["generation"] != generation:
                old = {
                    "generation": generation,
                    "request": request["request"],
                    "target": request.get("target"),
                    "active": False,
                }
            if operation == "prepare" and not old.get("result"):
                # Validate the monitor → scheduler route before handing off the old observer.
                old["result"] = observe(directory, old)
                old["observed_at"] = time.time()
            # Repeated prepare must not undo a committed enable after a lost response.
            if operation == "enable":
                old["active"] = True
            write(path, old)
        elif operation == "disable" and old:
            old["active"] = False
            old["generation"] = generation
            write(path, old)
    if operation == "enable":
        # Any competing start exits at the kernel lock; exactly one observer owns this spool.
        source = request["worker"]
        subprocess.Popen(
            [sys.executable, "-u", "-c", source],
            env={**os.environ, "CHEM_MONITOR_HOME": str(directory)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    return old or {"missing": True}


def main() -> None:
    if os.environ.get("CHEM_MONITOR_HOME"):
        worker(Path(os.environ["CHEM_MONITOR_HOME"]))
    else:
        try:
            print(json.dumps(control(json.load(sys.stdin))), flush=True)
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)


if __name__ == "__main__":
    main()
