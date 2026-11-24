"""Discover and select local native Agent executables without locking to stale PATH entries."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

SEMVER_PATTERN = re.compile(r"\b(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?\b")
_VERSION_CACHE: dict[tuple[str, int, int], str | None] = {}


def codex_search_dirs(home: Path | None = None) -> tuple[Path, ...]:
    base = home or Path.home()
    return (
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        base / ".local/bin",
        base / ".npm-global/bin",
        base / ".volta/bin",
        base / ".cargo/bin",
        base / "Applications/Codex.app/Contents/Resources",
        Path("/Applications/Codex.app/Contents/Resources"),
    )


def antigravity_search_dirs(home: Path | None = None) -> tuple[Path, ...]:
    base = home or Path.home()
    return (
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        base / ".local/bin",
    )


def parse_semver(text: str) -> tuple[tuple[int, int, int], int, tuple[tuple[int, int | str], ...]] | None:
    """Return a SemVer 2.0.0 comparison key where stable releases outrank prereleases."""
    match = SEMVER_PATTERN.search(text or "")
    if not match:
        return None
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease_raw = match.group(4)
    if not prerelease_raw:
        return (core, 1, ())
    parts: list[tuple[int, int | str]] = []
    for token in prerelease_raw.split("."):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token))
    return (core, 0, tuple(parts))


def extract_version(text: str) -> str | None:
    match = SEMVER_PATTERN.search(text or "")
    if not match:
        return None
    version = f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
    if match.group(4):
        version = f"{version}-{match.group(4)}"
    return version


def prepend_executable_dir(environment: dict[str, str], executable: str) -> dict[str, str]:
    """Ensure the chosen executable's directory precedes stale binaries on PATH."""
    target = Path(executable)
    if not target.is_absolute():
        resolved = shutil.which(executable, path=environment.get("PATH"))
        if not resolved:
            return environment
        target = Path(resolved)
    directory = str(target.parent)
    if not directory:
        return environment
    existing = [entry for entry in environment.get("PATH", "").split(os.pathsep) if entry]
    deduped = [directory, *(entry for entry in existing if entry != directory)]
    environment["PATH"] = os.pathsep.join(deduped)
    return environment


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _candidate_paths(
    command: str,
    fallback_dirs: tuple[Path, ...],
    *,
    path_env: str | None = None,
) -> list[Path]:
    raw_path = os.environ.get("PATH", "") if path_env is None else path_env
    directories: list[Path] = []
    seen_dirs: set[str] = set()
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        expanded = str(Path(entry).expanduser())
        if expanded not in seen_dirs:
            seen_dirs.add(expanded)
            directories.append(Path(expanded))
    for directory in fallback_dirs:
        key = str(directory)
        if key not in seen_dirs:
            seen_dirs.add(key)
            directories.append(directory)

    candidates: list[Path] = []
    seen_targets: set[str] = set()
    for directory in directories:
        candidate = directory / command
        if not _is_executable_file(candidate):
            continue
        try:
            real_key = str(candidate.resolve())
        except OSError:
            real_key = str(candidate)
        if real_key in seen_targets:
            continue
        seen_targets.add(real_key)
        candidates.append(candidate)
    return candidates


def probe_binary_version(executable: str | Path, *, timeout: float = 1.5) -> str | None:
    """Read `<binary> --version` once per (realpath, mtime, size); never raises."""
    path = Path(executable)
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return None
    cache_key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    if cache_key in _VERSION_CACHE:
        return _VERSION_CACHE[cache_key]

    env = prepend_executable_dir(os.environ.copy(), str(path))
    version: str | None = None
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
        if completed.returncode == 0:
            version = extract_version(completed.stdout) or extract_version(completed.stderr)
    except (OSError, subprocess.SubprocessError):
        version = None
    _VERSION_CACHE[cache_key] = version
    return version


def _rank_codex_candidates(
    candidates: list[Path],
    *,
    probe_single: bool = False,
) -> list[dict[str, str]]:
    if not candidates:
        return []
    should_probe = probe_single or len(candidates) > 1
    entries: list[tuple[tuple, int, dict[str, str]]] = []
    for index, candidate in enumerate(candidates):
        version = probe_binary_version(candidate) if should_probe else None
        parsed = parse_semver(version or "")
        sort_key = (
            1 if parsed is not None else 0,
            parsed if parsed is not None else ((0, 0, 0), 0, ()),
            -index,
        )
        entries.append((sort_key, index, {"path": str(candidate), "version": version or ""}))
    entries.sort(key=lambda item: item[0], reverse=True)
    return [item[2] for item in entries]


def discover_codex_candidates(
    *,
    path_env: str | None = None,
    home: Path | None = None,
) -> list[dict[str, str]]:
    """List unique local Codex executables sorted newest-first, with versions."""
    candidates = _candidate_paths("codex", codex_search_dirs(home), path_env=path_env)
    return _rank_codex_candidates(candidates, probe_single=True)


def resolve_codex_executable(
    configured: str,
    *,
    path_env: str | None = None,
    home: Path | None = None,
) -> str | None:
    """Resolve explicit paths verbatim; for default `codex`, prefer the newest installed binary."""
    raw = (configured or "").strip() or "codex"
    expanded = str(Path(raw).expanduser())
    if raw != "codex":
        found = shutil.which(expanded, path=path_env)
        if found:
            return found
        candidate = Path(expanded)
        return str(candidate) if _is_executable_file(candidate) else None

    candidates = _candidate_paths("codex", codex_search_dirs(home), path_env=path_env)
    if not candidates:
        return None
    if len(candidates) == 1:
        return str(candidates[0])
    ranked = _rank_codex_candidates(candidates, probe_single=False)
    return ranked[0]["path"] if ranked else None


def resolve_antigravity_executable(
    configured: str,
    *,
    path_env: str | None = None,
    home: Path | None = None,
) -> str | None:
    raw = (configured or "").strip() or "agy"
    expanded = str(Path(raw).expanduser())
    found = shutil.which(expanded, path=path_env)
    if found:
        return found
    if raw == "agy":
        for directory in antigravity_search_dirs(home):
            candidate = directory / "agy"
            if _is_executable_file(candidate):
                return str(candidate)
        return None
    candidate = Path(expanded)
    return str(candidate) if _is_executable_file(candidate) else None
