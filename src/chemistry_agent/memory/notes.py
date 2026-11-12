"""Author research notes without importing the agent tool registry."""

import hashlib
import json

from chemistry_agent.errors import AgentError
from chemistry_agent.memory.models import MemoryDocument, MemorySave
from chemistry_agent.memory.scope import MemoryScope, resolve_scope
from chemistry_agent.memory.service import ResearchMemory


async def save_note(memory: ResearchMemory, session: str | MemoryScope, args: MemorySave) -> dict:
    if bool(args.memory_id) != bool(args.expected_version):
        raise AgentError("memory_revision_required", "修改需同时提供记忆 ID 和版本。", 422)
    digest = hashlib.sha256(
        json.dumps(
            args.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    source_session, source_key = session, f"note:{digest}"
    if args.memory_id:
        previous = await memory.read(session, str(args.memory_id))
        if not previous["source_key"].startswith("note:"):
            raise AgentError(
                "memory_source_readonly", "自动记录保留原始证据，请另存补充文档。", 409
            )
        async with memory.store.lock:
            caller = await resolve_scope(memory.store, session)
        source_session = MemoryScope(
            caller.owner_id,
            previous["project_id"],
            previous["session_id"],
            previous["session_title"],
        )
        source_key = previous["source_key"]
    return await memory.save(
        source_session,
        MemoryDocument.model_validate(args.model_dump(exclude={"memory_id", "expected_version"})),
        source_key=source_key,
        expected_version=args.expected_version,
        evidence={
            "level": "unverified",
            "origin": "authored_note",
            "scientifically_validated": False,
        },
    )
