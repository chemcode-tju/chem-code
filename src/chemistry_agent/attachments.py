"""Account-scoped uploads. Bytes are stored as data and never executed."""

import json
from typing import TYPE_CHECKING
from urllib.parse import unquote
from uuid import UUID, uuid4

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import Response

from chemistry_agent.errors import AgentError

if TYPE_CHECKING:
    from chemistry_agent.store import Store

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARACTERS = 20000
router = APIRouter(prefix="/api/files", tags=["files"])


async def init_attachment_schema(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id),
            name TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL
        )
    """)


def text_preview(content: bytes) -> str | None:
    """Only expose valid UTF-8 text; arbitrary binary files remain downloadable."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if any(ord(character) < 32 and character not in "\n\r\t" for character in text):
        return None
    return text[:MAX_TEXT_CHARACTERS]


@router.post("", status_code=201)
async def upload_file(request: Request) -> dict:
    name = unquote(request.headers.get("x-file-name", "file"))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if ord(character) >= 32)[:255] or "file"
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_FILE_BYTES:
            raise AgentError("file_too_large", "单个文件不能超过 25 MB。", 413)
        content.extend(chunk)
    file_id = str(uuid4())
    store = request.app.state.store
    async with store.transaction():
        await store.db.execute(
            "INSERT INTO attachments(id, owner_id, name, size, content) VALUES(?,?,?,?,?)",
            (file_id, request.state.user_id, name, len(content), bytes(content)),
        )
    preview = text_preview(bytes(content))
    return {
        "id": file_id,
        "name": name,
        "size": len(content),
        "text_available": preview is not None,
    }


@router.get("/{file_id}")
async def download_file(file_id: UUID, request: Request) -> Response:
    from urllib.parse import quote

    store = request.app.state.store
    async with store.lock:
        row = await store._one(
            "SELECT name, content FROM attachments WHERE id=? AND owner_id IS ?",
            (str(file_id), request.state.user_id),
        )
    if row is None:
        raise AgentError("file_not_found", "文件不存在。", 404)
    return Response(
        content=row["content"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['name'])}",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def attachment_context(store: "Store", file_ids: list[UUID], owner_id: str | None) -> str:
    """Resolve only the current account's files before starting a model run."""
    files = []
    remaining = 40000
    async with store.lock:
        for file_id in dict.fromkeys(file_ids):
            row = await store._one(
                "SELECT name, size, content FROM attachments WHERE id=? AND owner_id IS ?",
                (str(file_id), owner_id),
            )
            if row is None:
                raise AgentError("file_not_found", "文件不存在。", 404)
            preview = text_preview(row["content"])
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
