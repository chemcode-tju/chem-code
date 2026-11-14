"""Session-owned local outputs, independent of the Agent and calculation backend."""

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from chemistry_agent.attachments import text_preview
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext

router = APIRouter(prefix="/api/workspaces", tags=["workspace outputs"])
OUTPUT_SUFFIXES = {
    ".vasp",
    ".poscar",
    ".contcar",
    ".xdatcar",
    ".data",
    ".lmp",
    ".cell",
    ".xtc",
    ".trr",
    ".top",
    ".itp",
    ".xyz",
    ".extxyz",
    ".cif",
    ".pdb",
    ".gro",
    ".traj",
    ".dump",
    ".lammpstrj",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".pdf",
    ".zip",
    ".csv",
    ".tsv",
    ".json",
    ".txt",
    ".md",
    ".py",
    ".sh",
    ".inp",
    ".in",
    ".out",
    ".log",
}
INTERNAL_FILES = {"platform-history.json", "credentials.json", "auth.json", "config.json"}
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SCAN_FILES = 512


class PublishFiles(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=128)


class WorkspaceFiles:
    def __init__(self, store, root: Path) -> None:
        self.store, self.root = store, root.resolve()
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.store.transaction():
            await self.store.db.execute("""
                CREATE TABLE IF NOT EXISTS workspace_outputs (
                    file_id TEXT PRIMARY KEY REFERENCES attachments(id),
                    session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    UNIQUE(session_id,path,sha256)
                )
            """)

    def workspace(self, session: str) -> Path:
        return self.root / str(UUID(session))

    def source(self, session: str, name: str) -> Path:
        root = self.workspace(session)
        path = Path(name)
        if not path.is_absolute():
            path = root / path
        try:
            relative = path.relative_to(root)
            if not relative.parts or any(
                part in {"..", "archive", "__pycache__", "node_modules"}
                or part.startswith(".")
                or part in INTERNAL_FILES
                for part in relative.parts
            ):
                raise ValueError
            # A linked session or parent directory must not expose another session/host file.
            if root.is_symlink() or any(
                parent.is_symlink()
                for parent in [path, *path.parents]
                if parent != self.root and self.root in parent.parents
            ):
                raise ValueError
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise AgentError(
                "workspace_path_invalid", "只能发布当前对话工作目录中的成果文件。", 422
            ) from exc
        return path

    def candidates(self, session: str) -> list[str]:
        root = self.workspace(session)
        if not root.is_dir() or root.is_symlink():
            return []
        result = []
        for directory, folders, names in os.walk(root, followlinks=False):
            folders[:] = sorted(
                name
                for name in folders
                if not name.startswith(".")
                and name not in {"archive", "__pycache__", "node_modules"}
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                path = Path(directory) / name
                if name.startswith(".") or name in INTERNAL_FILES or path.is_symlink():
                    continue
                if path.suffix.lower() in OUTPUT_SUFFIXES or name in {
                    "POSCAR",
                    "CONTCAR",
                    "XDATCAR",
                }:
                    result.append(str(path.relative_to(root)))
                    if len(result) >= MAX_SCAN_FILES:
                        return result
        return result

    def snapshot(self, source: Path, target: Path) -> tuple[str, int, str | None, int]:
        """Copy original bytes once; never serve a mutable source path to the browser."""
        digest, size, prefix = hashlib.sha256(), 0, b""
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise AgentError("workspace_file_size", "文件超过 128 MiB 或不是普通文件。", 422)
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise AgentError("workspace_file_size", "文件超过 128 MiB。", 422)
                digest.update(chunk)
                writer.write(chunk)
                if len(prefix) < 20000:
                    prefix += chunk[: 20000 - len(prefix)]
            after = os.fstat(reader.fileno())
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise AgentError("workspace_file_changing", "文件仍在写入，请完成后发布。", 409)
        return digest.hexdigest(), size, text_preview(prefix), after.st_mtime_ns

    async def publish(self, session: str, paths: list[str], run_id: str | None = None) -> dict:
        async with self.lock:
            async with self.store.lock:
                owner = await self.store._one(
                    "SELECT owner_id FROM sessions WHERE id=?", (session,)
                )
            if owner is None:
                raise AgentError("session_not_found", "对话不存在。", 404)
            names = paths or await asyncio.to_thread(self.candidates, session)
            errors = []
            for name in dict.fromkeys(names):
                file_id = str(uuid4())
                target = self.store.attachment_dir / file_id
                try:
                    source = self.source(session, name)
                    relative = str(source.relative_to(self.workspace(session)))
                    metadata = await asyncio.to_thread(source.stat)
                    async with self.store.lock:
                        cached = await self.store._one(
                            "SELECT file_id FROM workspace_outputs WHERE session_id=? AND path=? "
                            "AND size=? AND modified_ns=?",
                            (session, relative, metadata.st_size, metadata.st_mtime_ns),
                        )
                    if cached:
                        continue
                    digest, size, excerpt, modified = await asyncio.to_thread(
                        self.snapshot, source, target
                    )
                    async with self.store.transaction():
                        existing = await self.store._one(
                            "SELECT file_id FROM workspace_outputs "
                            "WHERE session_id=? AND path=? AND sha256=?",
                            (session, relative, digest),
                        )
                        if existing:
                            await self.store.db.execute(
                                "UPDATE workspace_outputs SET modified_ns=? WHERE file_id=?",
                                (modified, existing["file_id"]),
                            )
                            target.unlink(missing_ok=True)
                            continue
                        # Backfill to the producing run, not the latest reply.
                        producing_run = run_id
                        if producing_run is None:
                            row = await self.store._one(
                                "SELECT id FROM runs WHERE session_id=? AND julianday(started_at) "
                                "<= julianday(?, 'unixepoch') ORDER BY started_at DESC LIMIT 1",
                                (session, modified / 1e9),
                            )
                            producing_run = row["id"] if row else None
                        await self.store.db.execute(
                            "INSERT INTO attachments"
                            "(id,owner_id,name,size,content,on_disk,excerpt) "
                            "VALUES(?,?,?,?,X'',1,?)",
                            (file_id, owner["owner_id"], source.name, size, excerpt),
                        )
                        await self.store.db.execute(
                            "INSERT INTO session_files VALUES(?,?)", (session, file_id)
                        )
                        await self.store.db.execute(
                            "INSERT INTO workspace_outputs VALUES(?,?,?,?,?,?,?)",
                            (file_id, session, producing_run, relative, digest, size, modified),
                        )
                except (AgentError, OSError) as exc:
                    target.unlink(missing_ok=True)
                    errors.append({"path": name, "message": str(exc)})
            return {
                "files": await self.list(session),
                "errors": errors,
                "scan_limit_reached": not paths and len(names) >= MAX_SCAN_FILES,
            }

    async def list(self, session: str) -> list[dict]:
        async with self.store.lock:
            async with self.store.db.execute(
                "SELECT w.*,a.name,a.excerpt IS NOT NULL AS text_available "
                "FROM workspace_outputs w "
                "JOIN attachments a ON a.id=w.file_id WHERE w.session_id=? "
                "ORDER BY w.modified_ns,w.rowid",
                (session,),
            ) as cursor:
                rows = await cursor.fetchall()
        latest = {row["path"]: dict(row) for row in rows}
        return [
            {
                **row,
                "id": row["file_id"],
                "download_url": f"/api/files/{row['file_id']}",
                "source_path": str(self.workspace(session) / row["path"]),
            }
            for row in latest.values()
        ]


def workspace_tools(service) -> list[Tool]:
    async def publish(args: PublishFiles, context: ToolContext) -> dict:
        return await service().publish(context.session_id, args.paths, context.run_id)

    return [
        Tool(
            "workspace_publish",
            "Publish session workspace files as immutable downloads and web previews. "
            "paths=[] discovers outputs. Returns download_url and reusable file IDs.",
            PublishFiles,
            publish,
            True,
        )
    ]


@router.get("/{session_id}/files")
async def list_outputs(session_id: UUID, request: Request) -> dict:
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    return {"files": await request.app.state.workspace_files.list(str(session_id))}


@router.post("/{session_id}/publish")
async def publish_outputs(session_id: UUID, body: PublishFiles, request: Request) -> dict:
    await request.app.state.store.require_session(str(session_id), request.state.user_id)
    return await request.app.state.workspace_files.publish(str(session_id), body.paths)
