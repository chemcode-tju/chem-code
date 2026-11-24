"""Stdlib-only SSH bootstrap. One JSON envelope precedes the native protocol stream."""

import fcntl
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def location(value: str) -> Path:
    path = (Path.home() / value[2:] if value.startswith("~/") else Path(value)).resolve()
    if any(part.casefold() == "archive" for part in path.parts):
        raise ValueError("archive is not a runtime directory")
    return path


def _parse_semver(text: str):
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?\b", text or "")
    if not match:
        return None
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4)
    if not prerelease:
        return (core, 1, ())
    parts = tuple((0, int(tok)) if tok.isdigit() else (1, tok) for tok in prerelease.split("."))
    return (core, 0, parts)


def _discover_executable(
    executable: str, prefer_newest: bool = False, fallback_dirs=None
) -> str | None:
    home = Path.home()
    dirs = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if fallback_dirs is None:
        fallback_dirs = (
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
            home / ".local/bin",
            home / ".npm-global/bin",
            home / ".volta/bin",
            home / ".cargo/bin",
            home / "Applications/Codex.app/Contents/Resources",
            Path("/Applications/Codex.app/Contents/Resources"),
        )
    dirs.extend(fallback_dirs)
    candidates = []
    seen_targets = set()
    for directory in dirs:
        candidate = directory / executable
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        candidates.append(candidate)
    if not candidates:
        return None
    if len(candidates) == 1 or not prefer_newest:
        return str(candidates[0])
    ranked = []
    for index, candidate in enumerate(candidates):
        parsed = None
        try:
            env = os.environ.copy()
            env["PATH"] = f"{candidate.parent}{os.pathsep}{env.get('PATH', '')}"
            proc = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=1.5,
                check=False,
                env=env,
            )
            if proc.returncode == 0:
                parsed = _parse_semver(proc.stdout) or _parse_semver(proc.stderr)
        except (OSError, subprocess.SubprocessError):
            parsed = None
        ranked.append(((1 if parsed else 0, parsed or ((0, 0, 0), 0, ()), -index), str(candidate)))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def discover() -> dict:
    agents = []
    for backend, name, executable in (
        ("codex", "Codex", "codex"),
        ("antigravity", "Antigravity", "agy"),
    ):
        path = _discover_executable(executable, prefer_newest=(backend == "codex"))
        if path:
            agents.append({"backend": backend, "name": name, "executable": path})
    return {"agents": agents, "home": str(Path.home()), "hostname": os.uname().nodename}


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
            else:
                result = prepare(request)
            print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)


if __name__ == "__main__":
    main()
