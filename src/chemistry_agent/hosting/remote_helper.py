"""Stdlib-only SSH bootstrap. One JSON envelope precedes the native protocol stream."""

import fcntl
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def location(value: str) -> Path:
    path = (Path.home() / value[2:] if value.startswith("~/") else Path(value)).resolve()
    if "archive" in path.parts:
        raise ValueError("archive is not a runtime directory")
    return path


def discover() -> dict:
    agents = []
    for backend, name, executable in (
        ("codex", "Codex", "codex"),
        ("antigravity", "Antigravity", "agy"),
    ):
        path = shutil.which(executable)
        for candidate in (
            Path.home() / ".local/bin" / executable,
            Path("/opt/homebrew/bin") / executable,
        ):
            if not path and candidate.is_file() and os.access(candidate, os.X_OK):
                path = str(candidate)
        if path:
            agents.append({"backend": backend, "name": name, "executable": path})
    return {"agents": agents, "home": str(Path.home()), "hostname": os.uname().nodename}


def directories(value: str = "") -> dict:
    """Browse folder names only; choosing a folder never creates or changes it."""
    path = location(value or "~/")
    if path.exists() and not path.is_dir():
        raise ValueError("请选择文件夹")
    items = []
    truncated = False
    if path.is_dir():
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.name == "archive":
                    continue
                if entry.is_dir(follow_symlinks=False):
                    items.append({"name": entry.name, "path": str(path / entry.name)})
                    if len(items) == 200:
                        truncated = True
                        break
    return {
        "path": str(path),
        "parent": str(path.parent),
        "exists": path.is_dir(),
        "directories": sorted(items, key=lambda item: item["name"].casefold()),
        "truncated": truncated,
    }


def output_manifest(request: dict) -> dict:
    """Bound metadata discovery; incomplete baselines must never imply new files."""
    root = location(request["workspace"])
    files = {}
    count = 0
    deadline = time.monotonic() + 2
    excluded = set(request["internal"]) | {"archive", "node_modules", "__pycache__"}

    def failed(error):
        raise error

    try:
        for directory, folders, names in os.walk(root, followlinks=False, onerror=failed):
            if time.monotonic() > deadline:
                return {"files": {}, "complete": False}
            folders[:] = sorted(
                name
                for name in folders
                if name not in excluded
                and not name.startswith(".")
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                count += 1
                if count > 10000 or time.monotonic() > deadline:
                    return {"files": {}, "complete": False}
                path = Path(directory) / name
                if name.startswith(".") or name in excluded or path.is_symlink():
                    continue
                if path.suffix.lower() not in request["suffixes"] and name not in {
                    "POSCAR",
                    "CONTCAR",
                    "XDATCAR",
                }:
                    continue
                info = path.stat()
                files[str(path.relative_to(root))] = [
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ]
        return {"files": files, "complete": True}
    except OSError:
        return {"files": {}, "complete": False}


def prepare(request: dict) -> dict:
    home = location(request["home"])
    workspace = location(request["workspace"])
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    backend = request["backend"]
    source = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        if backend == "codex"
        else Path.home() / ".gemini"
    )
    # Private sessions, shared native credentials/settings. Never import desktop sessions.
    shared = (
        ("auth.json", "AGENTS.md", "skills", "rules", "policy")
        if backend == "codex"
        else ("skills", "rules", "config/plugins", "config/node_modules", "config/sidecars")
    )
    copied = (
        ("config.toml", "hooks.json")
        if backend == "codex"
        else (
            "settings.json",
            "GEMINI.md",
            "config/config.json",
            "config/mcp_config.json",
            "config/projects/default-cli-project.json",
        )
    )
    for name in shared:
        target = home / name
        if (source / name).exists() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source / name)
    for name in copied:
        target = home / name
        if (source / name).is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
            target.chmod(0o600)
    if "history" in request:
        history = workspace / request.get("history_name", "platform-history.json")
        if not history.resolve().is_relative_to(workspace):
            raise ValueError("History must remain in its working folder")
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(request["history"], ensure_ascii=False))
    return {"home": str(home), "workspace": str(workspace), "python": sys.executable}


def native(request: dict) -> None:
    """Keep the remote writer lock until its child has exited, including SSH EOF."""
    info = prepare(request)
    with (Path(info["home"]) / "platform-writer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        arguments = [str(Path(request["executable"]).expanduser()), *request["arguments"]]
        if request["backend"] == "antigravity":
            arguments.extend(
                [
                    "--gemini-dir",
                    info["home"],
                    "--app-data-dir",
                    "antigravity-cli",
                    "--process-per-session",
                ]
            )
        environment = os.environ.copy()
        if request["backend"] == "codex":
            environment["CODEX_HOME"] = info["home"]
        process = subprocess.Popen(
            arguments,
            cwd=info["workspace"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(lock.fileno(),),
        )

        def stop(signum, frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGHUP, stop)
        print(json.dumps({"ready": True, **info}), flush=True)
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ, process.stdin.fileno())
        selector.register(process.stdout.fileno(), selectors.EVENT_READ, sys.stdout.fileno())
        try:
            while process.poll() is None:
                for key, _ in selector.select(timeout=1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        return
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(key.data, view) :]
        finally:
            selector.close()
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def envelope() -> dict:
    # Read exactly one line: buffered stdin can swallow the first protocol request.
    data = bytearray()
    while (byte := os.read(sys.stdin.fileno(), 1)) != b"\n":
        if not byte:
            raise EOFError("Missing request")
        data.extend(byte)
    return json.loads(data)


def main() -> None:
    try:
        request = envelope()
        if request["operation"] == "native":
            native(request)
        else:
            if request["operation"] == "discover":
                result = discover()
            elif request["operation"] == "outputs":
                result = output_manifest(request)
            elif request["operation"] == "directories":
                result = directories(request.get("path", ""))
            else:
                result = prepare(request)
            print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)


if __name__ == "__main__":
    main()
