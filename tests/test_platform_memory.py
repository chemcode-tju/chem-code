"""Memory survives conversation removal and is usable without any model call."""

import json
import sqlite3
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.memory.models import MemoryDocument, MemorySave, MemorySearch
from chemistry_agent.memory.notes import save_note
from chemistry_agent.memory.scope import MemoryScope
from chemistry_agent.memory.service import ResearchMemory
from chemistry_agent.store import Store


async def test_research_memory_survives_source_deletion_and_can_be_revised(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = await Store.open(path)
    session = (await store.create_session("Cu 周期回卷核对"))["id"]
    memory = ResearchMemory(store)
    note = MemorySave(title="周期性比较", content="使用最小镜像检查晶胞边界回卷。")
    saved = await save_note(memory, session, note)
    async with store.transaction():
        await store.db.execute("DELETE FROM sessions WHERE id=?", (session,))
    await store.close()
    store = await Store.open(path)
    try:
        memory = ResearchMemory(store)
        scope = MemoryScope()
        item = await memory.read(scope, saved["id"])
        assert item["session_id"] == session
        assert item["session_title"] == "Cu 周期回卷核对"
        assert (await memory.search(scope, MemorySearch(query="周期")))["items"]
        updated = await save_note(
            memory,
            scope,
            note.model_copy(
                update={
                    "memory_id": UUID(saved["id"]),
                    "expected_version": 1,
                    "content": "先核对晶胞、原子对应和周期轴，再使用最小镜像。",
                }
            ),
        )
        assert updated["id"] == saved["id"] and updated["version"] == 2
        assert len(await memory.versions(scope, saved["id"])) == 2
        assert not (await store._one("SELECT COUNT(*) n FROM context_memory"))["n"]
    finally:
        await store.close()


def test_platform_api_without_chat_or_provider(settings_factory):
    def unexpected(request):
        raise AssertionError("Research platform must not call the model")

    with TestClient(create_app(settings_factory(), transport=httpx.MockTransport(unexpected))) as c:
        project = c.post("/api/projects", json={"name": "Cu 表面"}).json()["id"]
        root = "/api/research/memories"
        note = {"title": "CO adsorption", "content": "来自实际结构的方案", "systems": ["Cu"]}
        saved = c.post(root, params={"project_id": project}, json=note)
        assert saved.status_code == 200, saved.text
        identifier = saved.json()["id"]
        assert c.get("/sessions").json() == []
        assert not c.get(root).json()["items"]
        assert c.get(root, params={"project_id": project}).json()["items"][0]["id"] == identifier
        assert c.get(root, params={"scope": "personal"}).json()["items"]
        full = c.get(f"{root}/{identifier}").json()
        assert full["session_id"] is None and full["project_id"] == project
        assert full["evidence"]["scientifically_validated"] is False
        assert c.get(f"{root}/{identifier}/document.md").status_code == 200
        assert len(c.get(f"{root}/{identifier}/versions").json()) == 1
        assert c.get(f"{root}/{identifier}/versions/1").json()["content"] == note["content"]
        updated = c.post(
            root,
            json=note
            | {
                "memory_id": identifier,
                "expected_version": 1,
                "content": "新增证据",
            },
        )
        assert updated.json()["version"] == 2
        assert (
            c.post(f"{root}/{identifier}/forget", json={"expected_version": 2}).status_code == 204
        )
        assert c.get(f"{root}/{identifier}").status_code == 404
        assert c.get(root, params={"scope": "session"}).status_code == 422


async def test_legacy_migration_preserves_versions_index_and_source(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = await Store.open(path)
    session = (await store.create_session("原计算"))["id"]
    memory = ResearchMemory(store)
    document = MemoryDocument(title="CO Cu100", content="优化后比较吸附能")
    saved = await memory.save(session, document, source_key="plan:original")
    await memory.save(
        session, document.model_copy(update={"content": "更新后"}), source_key="plan:original"
    )
    await store.close()
    # Reconstruct the pre-upgrade table, retaining all original version/FTS rows.
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE legacy_memories (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                source_key TEXT NOT NULL, version INTEGER NOT NULL,
                document TEXT NOT NULL, digest TEXT NOT NULL, forgotten INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id,source_key)
            );
            INSERT INTO legacy_memories SELECT id,session_id,source_key,version,document,digest,
                forgotten,created_at,updated_at FROM research_memories;
            DROP TABLE research_memories;
            ALTER TABLE legacy_memories RENAME TO research_memories;
        """)
    store = await Store.open(path)
    try:
        memory = ResearchMemory(store)
        item = await memory.read(MemoryScope(), saved["id"])
        assert item["version"] == 2 and item["content"] == "更新后"
        assert item["session_title"] == "原计算"
        assert (await memory.search(MemoryScope(), MemorySearch(query="Cu100")))["items"]
        assert len(await memory.versions(MemoryScope(), saved["id"])) == 2
        assert (await memory.version(MemoryScope(), saved["id"], 1))["content"] == document.content
        async with store.db.execute("PRAGMA foreign_key_check") as cursor:
            assert not await cursor.fetchall()
        assert json.loads(
            (await store._one("SELECT document FROM research_memories WHERE id=?", (saved["id"],)))[
                "document"
            ]
        )["evidence"] == {"level": "unverified"}
    finally:
        await store.close()
