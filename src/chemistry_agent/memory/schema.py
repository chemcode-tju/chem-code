"""Upgrade research ownership without changing document IDs, versions or FTS terms."""

import aiosqlite


async def detach_session_ownership(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(research_memories)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "namespace" in columns:
        return
    # SQLite table rebuild: keep the parent table name so version FKs stay valid.
    # Startup owns the database lock; no runtime requests can observe this migration.
    await db.commit()
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("""
            CREATE TABLE research_memories_new (
                id TEXT PRIMARY KEY, session_id TEXT,
                source_key TEXT NOT NULL, version INTEGER NOT NULL,
                document TEXT NOT NULL, digest TEXT NOT NULL,
                forgotten INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                owner_id TEXT, project_id TEXT, source_title TEXT NOT NULL DEFAULT '',
                namespace TEXT NOT NULL, UNIQUE(namespace, source_key)
            )
        """)
        await db.execute("""
            INSERT INTO research_memories_new
            SELECT m.*, s.owner_id, ps.project_id, COALESCE(s.title,''), 'session:' || m.session_id
            FROM research_memories m LEFT JOIN sessions s ON s.id=m.session_id
            LEFT JOIN project_sessions ps ON ps.session_id=m.session_id
        """)
        await db.execute("DROP TABLE research_memories")
        await db.execute("ALTER TABLE research_memories_new RENAME TO research_memories")
        await db.execute(
            "CREATE INDEX research_memories_owner_project ON research_memories(owner_id,project_id)"
        )
        async with db.execute("PRAGMA foreign_key_check(research_memory_versions)") as cursor:
            if await cursor.fetchone():
                raise RuntimeError("科研记忆迁移失败：版本记录缺少对应文档。")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")
