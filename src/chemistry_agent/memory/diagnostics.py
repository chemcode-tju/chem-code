"""Bounded historical comparisons for the legacy single-job diagnostic worker."""

import json
import logging
from typing import TYPE_CHECKING

from chemistry_agent.memory.models import MemorySearch
from chemistry_agent.memory.service import ResearchMemory

if TYPE_CHECKING:
    from chemistry_agent.store import Store

logger = logging.getLogger(__name__)


async def diagnostic_references(
    store: "Store | None", session: str, job: dict, preview: dict,
) -> dict:
    if store is None or not getattr(store, "research_memory_enabled", True):
        return {"items": []}
    query = " ".join(
        str(value)
        for value in (
            job.get("label", ""),
            job.get("exit_message", ""),
            preview.get("structure", {}).get("formula", ""),
        )
    ).strip()
    if not query:
        return {"items": []}
    try:
        memory = ResearchMemory(store)
        hits = await memory.search(session, MemorySearch(query=query[:2000], limit=6))
        items = []
        for hit in hits["items"]:
            if hit["evidence"].get("job_id") == job.get("id"):
                continue
            parameters = {
                key: value for key, value in preview["request"].items() if key != "structure"
            }
            comparison = await memory.compare(session, hit["id"], parameters)
            item = {
                "id": hit["id"],
                "version": hit["version"],
                "title": hit["title"],
                "excerpt": hit["excerpt"][:300],
                "evidence": hit["evidence"],
                "differences": comparison["differences"][:20],
                "differences_truncated": len(comparison["differences"]) > 20,
            }
            if len(json.dumps([*items, item], ensure_ascii=False).encode()) > 6000:
                continue
            items.append(item)
            if len(items) == 3:
                break
        return {"items": items, "caution": "历史对照不是执行授权，参数差异不证明根因。"}
    except Exception:
        logger.exception("Research memory diagnosis lookup unavailable")
        return {"items": [], "unavailable": True}
