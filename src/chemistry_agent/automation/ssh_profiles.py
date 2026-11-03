"""Service-local connection references. Private keys and passwords never enter these files."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from chemistry_agent.errors import AgentError


def write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temporary:
        try:
            json.dump(data, temporary, ensure_ascii=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            Path(temporary.name).unlink(missing_ok=True)
            raise
    Path(temporary.name).replace(path)


def pin_path(root: Path, hostname: str, port: int) -> Path:
    identity = hashlib.sha256(f"{hostname.lower()}:{port}".encode()).hexdigest()
    return root / "ssh" / "hosts" / f"{identity}.json"


def check_path(root: Path, identifier: str) -> Path:
    return root / "ssh" / "checks" / f"{UUID(identifier)}.json"


def last_check(root: Path, identifier: str) -> dict | None:
    path = check_path(root, identifier)
    return json.loads(path.read_text()) if path.exists() else None


def profile_path(root: Path, identifier: str) -> Path:
    return root / "ssh" / "connections" / f"{UUID(identifier)}.json"


def load_profile(root: Path, identifier: str) -> dict:
    try:
        return json.loads(profile_path(root, identifier).read_text())
    except FileNotFoundError as exc:
        raise AgentError("ssh_profile_missing", "此服务器没有托管的 SSH 配置。", 404) from exc


def shell_profiles(root: Path) -> dict[str, str]:
    result = {}
    for path in (root / "ssh" / "connections").glob("*.json"):
        item = json.loads(path.read_text())
        result[item["shell_alias"]] = item["id"]
    return result
