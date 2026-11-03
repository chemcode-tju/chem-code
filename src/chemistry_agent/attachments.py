"""Account-scoped original files with bounded text excerpts for model context."""

import asyncio
import codecs
import json
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

import aiosqlite
import anyio
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from chemistry_agent.errors import AgentError

if TYPE_CHECKING:
    from chemistry_agent.store import Store

MAX_TEXT_CHARACTERS = 20000
router = APIRouter(prefix="/api/files", tags=["files"])


async def init_attachment_schema(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id),
            name TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL
        )
    """)
    async with db.execute("PRAGMA table_info(attachments)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "on_disk" not in columns:
        await db.execute("ALTER TABLE attachments ADD COLUMN on_disk INTEGER NOT NULL DEFAULT 0")
        await db.execute("ALTER TABLE attachments ADD COLUMN excerpt TEXT")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS session_files (
            session_id TEXT NOT NULL REFERENCES sessions(id),
            file_id TEXT NOT NULL REFERENCES attachments(id), PRIMARY KEY(session_id, file_id)
        )
    """)


def _clean_text(text: str) -> bool:
    return all(char >= " " or char in "\n\r\t" for char in text)


def text_preview(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return text[:MAX_TEXT_CHARACTERS] if _clean_text(text) else None


@router.post("", status_code=201)
async def upload_file(request: Request) -> dict:
    name = unquote(request.headers.get("x-file-name", "file")).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if ord(character) >= 32)[:255] or "file"
    store = request.app.state.store
    file_id = str(uuid4())
    path = store.attachment_dir / file_id
    size, excerpt = 0, ""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True, mode=0o700)
    try:
        async with await anyio.open_file(path, "xb") as target:
            async for chunk in request.stream():
                size += len(chunk)
                await target.write(chunk)
                if decoder is not None:
                    try:
                        piece = decoder.decode(chunk, final=not chunk)
                        if _clean_text(piece):
                            excerpt += piece[: MAX_TEXT_CHARACTERS - len(excerpt)]
                        else:
                            decoder = None
                    except UnicodeDecodeError:
                        decoder = None
        async with store.transaction():
            await store.db.execute(
                "INSERT INTO attachments(id,owner_id,name,size,content,on_disk,excerpt) "
                "VALUES(?,?,?,?,X'',1,?)",
                (
                    file_id,
                    request.state.user_id,
                    name,
                    size,
                    excerpt if decoder is not None else None,
                ),
            )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"id": file_id, "name": name, "size": size, "text_available": decoder is not None}


@router.get("/{file_id}")
async def download_file(file_id: UUID, request: Request) -> Response:
    store = request.app.state.store
    async with store.lock:
        row = await store._one(
            "SELECT name, on_disk, content FROM attachments WHERE id=? AND owner_id IS ?",
            (str(file_id), request.state.user_id),
        )
    if row is None:
        raise AgentError("file_not_found", "文件不存在。", 404)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['name'])}",
        "X-Content-Type-Options": "nosniff",
    }
    if row["on_disk"]:
        path = store.attachment_dir / str(file_id)
        if not path.is_file():
            raise AgentError("file_not_found", "原始附件不存在。", 404)
        return FileResponse(path, media_type="application/octet-stream", headers=headers)
    return Response(content=row["content"], media_type="application/octet-stream", headers=headers)


async def attachment_context(store: "Store", file_ids: list[UUID], owner_id: str | None) -> str:
    """Resolve files for the current account without loading large originals into memory."""
    files, remaining = [], 40000
    async with store.lock:
        for file_id in dict.fromkeys(file_ids):
            row = await store._one(
                "SELECT name, size, on_disk, excerpt, content FROM attachments "
                "WHERE id=? AND owner_id IS ?",
                (str(file_id), owner_id),
            )
            if row is None:
                raise AgentError("file_not_found", "文件不存在。", 404)
            preview = row["excerpt"] if row["on_disk"] else text_preview(row["content"])
            text = preview[:remaining] if preview is not None else None
            remaining -= len(text or "")
            files.append(
                {
                    "id": str(file_id),
                    "name": row["name"],
                    "size": row["size"],
                    "text": text,
                    "text_available": preview is not None,
                    "note": "Text excerpt only; binary files are stored but not parsed.",
                }
            )
    return json.dumps({"uploaded_files": files}, ensure_ascii=False) if files else ""


async def session_file(store: "Store", session_id: str, file_id: UUID) -> tuple[str, Path | bytes]:
    """Only material explicitly attached to this session may become a calculation input."""
    async with store.lock:
        row = await store._one(
            "SELECT a.name,a.on_disk,a.content FROM sessions s "
            "JOIN attachments a ON a.id=? AND a.owner_id IS s.owner_id WHERE s.id=? AND ("
            "EXISTS (SELECT 1 FROM session_files WHERE session_id=s.id AND file_id=a.id) OR "
            "EXISTS (SELECT 1 FROM chat_requests q, json_each(q.body,'$.file_ids') f "
            "WHERE q.session_id=s.id AND q.status='completed' AND f.value=a.id))",
            (str(file_id), session_id),
        )
    if row is None:
        raise AgentError("file_not_found", "此对话中没有该附件。", 404)
    return row["name"], store.attachment_dir / str(file_id) if row["on_disk"] else row["content"]
