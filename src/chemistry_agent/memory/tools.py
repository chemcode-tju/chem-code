"""Model-facing memory operations derive ownership from the running session."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from chemistry_agent.memory.models import (
    MemoryCompare,
    MemoryRead,
    MemorySave,
    MemorySearch,
)
from chemistry_agent.memory.notes import save_note
from chemistry_agent.memory.service import ResearchMemory
from chemistry_agent.tools import Tool, ToolContext

if TYPE_CHECKING:
    from chemistry_agent.store import Store


def memory_tools(get_store: Callable[[], "Store"]) -> list[Tool]:
    async def search(args: MemorySearch, context: ToolContext) -> dict:
        return await ResearchMemory(get_store()).search(context.session_id, args)

    async def read(args: MemoryRead, context: ToolContext) -> dict:
        item = await ResearchMemory(get_store()).read(context.session_id, str(args.memory_id))
        content = item["content"]
        return item | {
            "content": content[args.offset : args.offset + args.length],
            "total_characters": len(content),
            "offset": args.offset,
            "truncated": args.offset > 0 or len(content) > args.offset + args.length,
        }

    async def save(args: MemorySave, context: ToolContext) -> dict:
        return await save_note(ResearchMemory(get_store()), context.session_id, args)

    async def compare(args: MemoryCompare, context: ToolContext) -> dict:
        return await ResearchMemory(get_store()).compare(
            context.session_id,
            str(args.memory_id),
            args.parameters,
        )

    return [
        Tool(
            "memory_search",
            "查科研记忆：体系、任务、报错关键词。默认当前项目；"
            "session 仅检索当前对话，personal 显式检索本人其他项目；offset 翻页。"
            "命中后用 memory_read 读来源。",
            MemorySearch,
            search,
            contextual=True,
        ),
        Tool(
            "memory_read",
            "读取科研记忆正文、精确参数和证据；可分段读取长文。",
            MemoryRead,
            read,
            contextual=True,
        ),
        Tool(
            "memory_save",
            "保存有用方案、方法或补充文档。明确体系及任务标签、适用条件和来源；"
            "手写内容始终未核验，不能伪造成功证据。修改需 ID 和 expected_version。",
            MemorySave,
            save,
            contextual=True,
        ),
        Tool(
            "memory_compare",
            "对比当前参数与历史参数，标出缺失项；差异不等于报错根因。",
            MemoryCompare,
            compare,
            contextual=True,
        ),
    ]
