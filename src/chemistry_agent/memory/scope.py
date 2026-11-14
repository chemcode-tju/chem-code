"""Research ownership is independent of an LLM conversation or provider."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chemistry_agent.errors import AgentError

if TYPE_CHECKING:
    from chemistry_agent.store import Store


@dataclass(frozen=True)
class MemoryScope:
    owner_id: str | None = None
    project_id: str | None = None
    source_session_id: str | None = None
    source_title: str = ""

    @property
    def namespace(self) -> str:
        if self.source_session_id:
            return f"session:{self.source_session_id}"
        return f"owner:{self.owner_id!r}:project:{self.project_id!r}"


async def resolve_scope(store: "Store", context: str | MemoryScope) -> MemoryScope:
    """Compatibility adapter: only legacy chat clients need a session lookup."""
    if isinstance(context, MemoryScope):
        return context
    row = await store._one(
        "SELECT s.owner_id, s.title, ps.project_id FROM sessions s "
        "LEFT JOIN project_sessions ps ON ps.session_id=s.id WHERE s.id=?",
        (context,),
    )
    if row is None:
        raise AgentError("session_not_found", "会话不存在。", 404)
    return MemoryScope(row["owner_id"], row["project_id"], context, row["title"])
