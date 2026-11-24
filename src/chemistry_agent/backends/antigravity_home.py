"""Private Gemini/Antigravity state, preserving native authentication and tools."""

import json
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from chemistry_agent.backends.codex_home import CodexHomes
from chemistry_agent.errors import AgentError


class AntigravityHomes:
    app_directory = "antigravity-cli"

    def __init__(self, source: Path | None = None, *, reuse_profile: bool = True) -> None:
        self.source = source or Path.home() / ".gemini"
        self.reuse_profile = reuse_profile

    def prepare(self, target: Path, thread: str | None = None) -> None:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.reuse_profile:
            self.copy_profile(target)
        if thread:
            self.import_session(target, thread)

    def copy_profile(self, target: Path) -> None:
        # Preserve user customizations, not conversation databases, caches or locks.
        # Native login uses the OS credential store; never copy OAuth tokens.
        for name in (
            "settings.json",
            "GEMINI.md",
            "config/config.json",
            "config/mcp_config.json",
            "config/projects/default-cli-project.json",
        ):
            source, destination = self.source / name, target / name
            if source.is_file() and not destination.exists():
                CodexHomes.copy_atomic(source, destination)
        for name in ("skills", "rules", "config/plugins", "config/node_modules", "config/sidecars"):
            source, destination = self.source / name, target / name
            if source.exists() and not destination.exists() and not destination.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(source)

    @staticmethod
    def require_idle(source: Path) -> None:
        """Do not snapshot a legacy conversation currently open in another process."""
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-t", "--", str(source), f"{source}-wal"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentError(
                "antigravity_migration_unavailable", "无法核对旧会话是否仍被使用，未迁移。", 409
            ) from exc
        if result.returncode == 0 or result.stdout.strip():
            raise AgentError(
                "antigravity_session_busy", "原 Antigravity 会话仍被进程使用，未迁移或重发。", 409
            )
        if result.returncode != 1:
            raise AgentError(
                "antigravity_migration_unavailable", "无法核对旧会话占用，未迁移。", 409
            )

    def import_session(self, target: Path, thread: str) -> None:
        """Import exactly one bound conversation using SQLite's WAL-aware backup."""
        if not re.fullmatch(r"[\w-]+", thread):
            raise ValueError("Invalid Antigravity session identifier")
        relative = Path(self.app_directory) / "conversations" / f"{thread}.db"
        destination, source = target / relative, self.source / relative
        if destination.exists():
            return
        if not source.is_file():
            raise AgentError(
                "antigravity_history_missing",
                "未找到可迁移的 Antigravity 会话记录，未创建空会话。",
                409,
            )
        self.require_idle(source)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{thread}.{uuid4().hex}.tmp")

        def signature() -> list[tuple[int, int] | None]:
            return [
                (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None
                for path in (source, source.with_name(f"{source.name}-wal"))
            ]

        before = signature()
        try:
            with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as incoming:
                ids = incoming.execute("SELECT DISTINCT cascade_id FROM trajectory_meta").fetchall()
                if ids != [(thread,)]:
                    raise AgentError("antigravity_history_invalid", "旧会话标识不匹配。", 409)
                with closing(sqlite3.connect(temporary)) as snapshot:
                    incoming.backup(snapshot)
            temporary.chmod(0o600)
            brain = self.source / self.app_directory / "brain" / thread
            if brain.is_dir():
                shutil.copytree(
                    brain,
                    target / self.app_directory / "brain" / thread,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("archive"),
                )
            annotation = Path(self.app_directory) / "annotations" / f"{thread}.pbtxt"
            if (self.source / annotation).is_file():
                CodexHomes.copy_atomic(self.source / annotation, target / annotation)
            self.require_idle(source)
            if before != signature():
                raise AgentError("antigravity_session_busy", "旧会话正在更新，未迁移或重发。", 409)
            temporary.replace(destination)
            marker = target / f"migration-{thread}.json"
            marker.write_text(json.dumps({"source": str(source), "thread_id": thread}))
            marker.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
