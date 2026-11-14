"""Existing durable record layout, shared by computation and chat compatibility APIs."""

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from chemistry_agent.errors import AgentError


class RecordRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def directory(self, session_id: str, kind: str) -> Path:
        if not kind.isidentifier() or kind == "archive":
            raise AgentError("record_kind_invalid", "无效的记录类型。", 422)
        directory = self.root / str(UUID(session_id)) / kind
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def save(self, session_id: str, kind: str, identifier: str, data: dict) -> None:
        path = self.directory(session_id, kind) / f"{UUID(identifier)}.json"
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".record-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, session_id: str, kind: str, identifier: str) -> dict:
        path = self.directory(session_id, kind) / f"{UUID(identifier)}.json"
        if not path.is_file():
            raise AgentError("artifact_not_found", "当前工作空间中不存在该记录。", 404)
        return json.loads(path.read_text())

    def list_records(
        self, session_id: str, kind: str, offset: int = 0, limit: int = 100
    ) -> list[dict]:
        paths = sorted(
            self.directory(session_id, kind).glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [json.loads(path.read_text()) for path in paths[offset : offset + limit]]
