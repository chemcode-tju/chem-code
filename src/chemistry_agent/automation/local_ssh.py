"""Discover existing OpenSSH aliases without importing keys or requiring AiiDA."""

import glob
import json
import re
import shlex
from pathlib import Path

from chemistry_agent.automation.ssh_profiles import write_private

ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def discover_aliases(config: Path | None = None) -> list[dict]:
    config = config or Path.home() / ".ssh" / "config"
    records: dict[str, dict] = {}
    visited: set[Path] = set()

    def read(path: Path, active: list[str], depth: int) -> list[str]:
        if depth > 8 or path in visited or "archive" in path.resolve().parts:
            return active
        visited.add(path)
        try:
            lines = path.read_text().splitlines()
        except (FileNotFoundError, PermissionError):
            return active
        for line in lines:
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not parts:
                continue
            key, _, inline = parts[0].partition("=")
            values = ([inline] if inline else []) + parts[1:]
            values = [value for value in values if value != "="]
            key = key.lower()
            if key == "include":
                for value in values:
                    pattern = Path(value).expanduser()
                    if not pattern.is_absolute():
                        pattern = config.parent / pattern
                    for included in sorted(glob.glob(str(pattern)))[:100]:
                        active = read(Path(included), active, depth + 1)
            elif key == "host":
                active = [value for value in values if ALIAS.fullmatch(value)]
                for alias in active:
                    records.setdefault(alias, {"alias": alias, "source": str(path)})
            elif key == "match":
                active = []
            elif key in {"hostname", "user", "port"} and values:
                for alias in active:
                    records[alias].setdefault(key, values[0])
        return active

    read(config, [], 0)
    return sorted(records.values(), key=lambda item: item["alias"])


def enabled_aliases(root: Path | None, defaults: tuple[str, ...] = ()) -> tuple[str, ...]:
    if root and (path := root / "ssh" / "enabled_aliases.json").exists():
        return tuple(json.loads(path.read_text())["hosts"])
    return defaults


def save_aliases(root: Path, hosts: list[str]) -> None:
    write_private(root / "ssh" / "enabled_aliases.json", {"hosts": sorted(set(hosts))})
