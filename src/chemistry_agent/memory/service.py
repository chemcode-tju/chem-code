"""Transactional Markdown documents with version history and a rebuildable FTS index."""

import hashlib
import json
import re
import unicodedata
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite

from chemistry_agent.errors import AgentError
from chemistry_agent.memory.models import MemoryDocument, MemorySearch, parameter_diff
from chemistry_agent.memory.schema import detach_session_ownership
from chemistry_agent.memory.scope import MemoryScope, resolve_scope

if TYPE_CHECKING:
    from chemistry_agent.store import Store


ALIASES = {
    "adsorption": ("吸附", "adsorption", "eads"),
    "reaction": ("反应", "reaction"),
    "barrier": ("能垒", "barrier", "neb", "过渡态"),
    "relaxation": ("优化", "relaxation", "relax"),
    "convergence": ("收敛", "convergence", "converged"),
}


def tokens(text: str) -> set[str]:
    """FTS terms for English/formulas and Chinese bigrams without an embedding service."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"([a-z][a-z0-9]*)\s*\(\s*(\d{3})\s*\)", r"\1\2", text)
    result = set(re.findall(r"[a-z0-9_]+", text))
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(span[i : i + 2] for i in range(len(span) - 1))
    for key, aliases in ALIASES.items():
        if any(alias in text for alias in aliases):
            result.add(key)
    return {word for word in result if len(word) > 1}


async def init_memory_schema(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS research_memories (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            source_key TEXT NOT NULL, version INTEGER NOT NULL,
            document TEXT NOT NULL, digest TEXT NOT NULL,
            forgotten INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(session_id, source_key)
        );
        CREATE TABLE IF NOT EXISTS research_memory_versions (
            memory_id TEXT NOT NULL REFERENCES research_memories(id),
            version INTEGER NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, version)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS research_memory_fts USING fts5(
            memory_id UNINDEXED, terms, tokenize='unicode61'
        );
    """)

    await detach_session_ownership(db)


class ResearchMemory:
    def __init__(self, store: "Store") -> None:
        self.store = store

    async def save(
        self,
        session: str | MemoryScope,
        document: MemoryDocument,
        *,
        source_key: str,
        evidence: dict | None = None,
        expected_version: int | None = None,
    ) -> dict:
        from chemistry_agent.store import utc_now

        body = document.model_dump() | {"evidence": evidence or {"level": "unverified"}}
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 300000:
            raise AgentError("memory_too_large", "记忆文档过大，请拆分。", 422)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.store.transaction():
            scope = await resolve_scope(self.store, session)
            old = await self.store._one(
                "SELECT * FROM research_memories WHERE namespace=? AND source_key=?",
                (scope.namespace, source_key),
            )
            if expected_version is not None and (old is None or old["version"] != expected_version):
                raise AgentError("memory_conflict", "记忆已更新，请重新读取。", 409)
            if old and old["forgotten"]:
                return {"id": old["id"], "forgotten": True}
            if old and old["digest"] == digest:
                return {"id": old["id"], "version": old["version"], "unchanged": True}
            identifier, version = (old["id"], old["version"] + 1) if old else (str(uuid4()), 1)
            now = utc_now()
            await self.store.db.execute(
                "INSERT INTO research_memories VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
                "document=excluded.document,digest=excluded.digest,updated_at=excluded.updated_at",
                (
                    identifier,
                    scope.source_session_id,
                    source_key,
                    version,
                    encoded,
                    digest,
                    old["created_at"] if old else now,
                    now,
                    scope.owner_id,
                    scope.project_id,
                    scope.source_title,
                    scope.namespace,
                ),
            )
            await self.store.db.execute(
                "INSERT INTO research_memory_versions VALUES(?,?,?,?)",
                (identifier, version, encoded, now),
            )
            await self.store.db.execute(
                "DELETE FROM research_memory_fts WHERE memory_id=?",
                (identifier,),
            )
            indexed = " ".join(sorted(tokens(encoded)))
            await self.store.db.execute(
                "INSERT INTO research_memory_fts VALUES(?,?)",
                (identifier, indexed),
            )
        return {"id": identifier, "version": version, "evidence": body["evidence"]}

    async def read(self, session: str | MemoryScope, identifier: str) -> dict:
        async with self.store.lock:
            caller = await resolve_scope(self.store, session)
            row = await self.store._one(
                "SELECT m.* FROM research_memories m "
                "WHERE m.id=? AND m.forgotten=0 AND m.owner_id IS ?",
                (identifier, caller.owner_id),
            )
        if row is None:
            raise AgentError("memory_not_found", "记忆不存在或无权读取。", 404)
        return dict(
            json.loads(row["document"]),
            id=row["id"],
            version=row["version"],
            session_id=row["session_id"],
            source_key=row["source_key"],
            project_id=row["project_id"],
            session_title=row["source_title"],
            updated_at=row["updated_at"],
        )

    async def search(self, session: str | MemoryScope, args: MemorySearch) -> dict:
        query_terms = sorted(tokens(args.query))[:80]
        facets = [term for term in query_terms if re.fullmatch(r"[a-z]{1,2}[0-9]{3}", term)]
        async with self.store.lock:
            caller = await resolve_scope(self.store, session)
            sql = "SELECT m.*, m.source_title AS session_title FROM research_memories m "
            params: list = []
            if query_terms:
                sql += "JOIN research_memory_fts f ON f.memory_id=m.id "
            sql += "WHERE m.forgotten=0 AND m.owner_id IS ? "
            params.append(caller.owner_id)
            if args.scope == "session":
                sql += "AND m.session_id=? "
                params.append(caller.source_session_id or "")
            elif args.scope == "project":
                sql += "AND m.project_id IS ? "
                params.append(caller.project_id)
            if args.kind:
                sql += "AND json_extract(m.document,'$.kind')=? "
                params.append(args.kind)
            if args.query.strip() and not query_terms:
                return {"items": [], "scope": args.scope, "truncated": False}
            if query_terms:
                sql += "AND research_memory_fts MATCH ? ORDER BY "
                params.append(" OR ".join(f'"{term}"' for term in query_terms))
                # Rank across the full authorized candidate set before LIMIT. Common task
                # words must not bury the explicitly requested material/facet in large corpora.
                if facets:
                    matches = [
                        "CASE WHEN instr(' ' || f.terms || ' ', ?) > 0 THEN 1 ELSE 0 END"
                        for _ in facets
                    ]
                    sql += "(" + " + ".join(matches) + ") DESC, "
                    params.extend(f" {term} " for term in facets)
                if set(query_terms) & ALIASES.keys():
                    sql += "CASE json_extract(m.document,'$.kind') "
                    sql += "WHEN 'plan' THEN 0 WHEN 'lesson' THEN 1 ELSE 2 END, "
                sql += "bm25(research_memory_fts), "
            else:
                sql += "ORDER BY "
            sql += "m.updated_at DESC, m.id LIMIT ? OFFSET ?"
            params.extend((args.limit + 1, args.offset))
            async with self.store.db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        hits = []
        for row in rows[: args.limit]:
            body = json.loads(row["document"])
            hits.append(
                {
                    "id": row["id"],
                    "version": row["version"],
                    "title": body["title"],
                    "kind": body["kind"],
                    "systems": body["systems"],
                    "tasks": body["tasks"],
                    "evidence": body["evidence"],
                    "excerpt": body["content"][:500],
                    "matched_terms": sorted(set(query_terms) & tokens(row["document"])),
                    "matched_facets": sorted(set(facets) & tokens(row["document"])),
                    "session_id": row["session_id"],
                    "session_title": row["session_title"],
                    "project_id": row["project_id"],
                    "updated_at": row["updated_at"],
                }
            )
        return {"items": hits, "scope": args.scope, "truncated": len(rows) > args.limit}

    async def versions(self, session: str | MemoryScope, identifier: str) -> list[dict]:
        await self.read(session, identifier)
        async with self.store.lock:
            async with self.store.db.execute(
                "SELECT version,created_at FROM research_memory_versions "
                "WHERE memory_id=? ORDER BY version DESC",
                (identifier,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def version(self, session: str | MemoryScope, identifier: str, version: int) -> dict:
        await self.read(session, identifier)
        async with self.store.lock:
            row = await self.store._one(
                "SELECT document FROM research_memory_versions WHERE memory_id=? AND version=?",
                (identifier, version),
            )
        if row is None:
            raise AgentError("memory_not_found", "版本不存在。", 404)
        return json.loads(row["document"]) | {"id": identifier, "version": version}

    async def forget(self, session: str | MemoryScope, identifier: str, version: int) -> None:
        await self.read(session, identifier)
        async with self.store.transaction():
            cursor = await self.store.db.execute(
                "UPDATE research_memories SET forgotten=1 WHERE id=? AND version=? AND forgotten=0",
                (identifier, version),
            )
            if cursor.rowcount != 1:
                raise AgentError("memory_conflict", "记忆已更新，请重新读取。", 409)
            await self.store.db.execute(
                "DELETE FROM research_memory_fts WHERE memory_id=?",
                (identifier,),
            )

    async def compare(self, session: str | MemoryScope, identifier: str, parameters: dict) -> dict:
        memory = await self.read(session, identifier)
        return {
            "memory_id": identifier,
            "version": memory["version"],
            "evidence": memory["evidence"],
            "applicability": memory["applicability"],
            "differences": parameter_diff(memory["parameters"], parameters),
            "caution": "差异是排查线索，不证明因果；缺失参数代表未知。复用前核对体系、"
            "泛函、赝势、单位、电荷/自旋、溶剂和结构约束。",
        }


def markdown(document: dict) -> str:
    """Export all metadata and exact parameters alongside human-readable prose."""
    metadata = {key: value for key, value in document.items() if key != "content"}
    return (
        f"# {document['title']}\n\n{document['content']}\n\n"
        "## 来源、适用范围与参数\n\n```json\n"
        + json.dumps(metadata, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
