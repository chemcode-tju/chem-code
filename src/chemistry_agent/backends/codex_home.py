"""Private Codex state per platform conversation; login remains Codex-owned."""

import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from chemistry_agent.errors import AgentError


class CodexHomes:
    def __init__(self, root: Path, source: Path | None = None) -> None:
        self.root = root.resolve()
        self.source = (
            source or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        ).resolve()

    def prepare(self, session: str) -> Path:
        if not re.fullmatch(r"[\w-]+", session):
            raise ValueError("Invalid platform session identifier")
        target = self.root / session
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
        # Never share SQLite, locks, sessions, caches or logs with the desktop.
        # Auth is a reference, not a copied refresh token that can go stale.
        for name in ("auth.json", "AGENTS.md", "rules", "policy", "skills"):
            source, link = self.source / name, target / name
            if source.exists() and not link.exists() and not link.is_symlink():
                link.symlink_to(source)
        for name in ("config.toml", "hooks.json"):
            source, destination = self.source / name, target / name
            if source.is_file() and not destination.exists():
                self.copy_atomic(source, destination)
        return target

    @staticmethod
    def copy_atomic(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                temporary.chmod(0o600)
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, output)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def import_thread(self, target: Path, thread: str) -> None:
        """Snapshot only the bound, idle legacy rollout. Never edit the original."""
        if not re.fullmatch(r"[\w-]+", thread):
            raise ValueError("Invalid Codex thread identifier")
        pattern = f"*/*/*/rollout-*-{thread}.jsonl"
        if next((target / "sessions").glob(pattern), None):
            return
        sources = list((self.source / "sessions").glob(pattern))
        if len(sources) != 1:
            raise AgentError(
                "codex_history_missing", "未找到可迁移的 Codex 会话记录；原会话未改动。", 409
            )
        source = sources[0]
        before = source.stat()
        active = set()
        metadata = None
        with source.open() as stream:
            for line in stream:
                item = json.loads(line)
                payload = item.get("payload", {})
                if item.get("type") == "session_meta":
                    metadata = payload.get("id")
                if item.get("type") != "event_msg":
                    continue
                kind = payload.get("type")
                turn = payload.get("turn_id") or payload.get("root_turn_id")
                if kind == "task_started":
                    active.add(turn)
                elif kind in {"task_complete", "turn_aborted"}:
                    active.discard(turn)
        if metadata != thread or active:
            raise AgentError(
                "codex_session_busy", "原 Codex 会话尚有未结束回合，未迁移或重发。", 409
            )
        destination = target / source.relative_to(self.source)
        self.copy_atomic(source, destination)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            destination.unlink()
            raise AgentError(
                "codex_session_busy", "原 Codex 会话正在更新，请在回合结束后重试。", 409
            )
        marker = target / f"migration-{thread}.json"
        marker.write_text(
            json.dumps({"source": str(source), "thread_id": thread, "size": after.st_size})
        )
        marker.chmod(0o600)
