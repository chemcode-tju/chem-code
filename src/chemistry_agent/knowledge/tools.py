"""Small, shared tool contracts; discovery is explicit, payloads stay on demand."""

import asyncio

from pydantic import BaseModel

from chemistry_agent.knowledge.assets import ASSET_TOOLS
from chemistry_agent.knowledge.config import KnowledgeQuery, KnowledgeRead, KnowledgeWrite
from chemistry_agent.knowledge.plugins import SkillQuery, SkillRead
from chemistry_agent.memory.scope import resolve_scope
from chemistry_agent.tools import Tool, ToolContext


class SkillMaterialize(BaseModel):
    skill: str


def knowledge_tools(get_service, workspace_for_session=None) -> list[Tool]:
    async def scope(context):
        service = get_service()
        async with service.store.lock:
            return await resolve_scope(service.store, context.session_id)

    async def search(args: KnowledgeQuery, context: ToolContext) -> dict:
        return await get_service().search(await scope(context), args)

    async def read(args: KnowledgeRead, context: ToolContext) -> dict:
        service = get_service()
        return await service.read(
            service.scope(await scope(context), args.scope), args.path, args.level
        )

    async def write(args: KnowledgeWrite, context: ToolContext) -> dict:
        service = get_service()
        return await service.write(
            service.scope(await scope(context), args.scope), args.path, args.content
        )

    async def skills(args: SkillQuery, context: ToolContext) -> dict:
        return await asyncio.to_thread(get_service().plugins.catalog, args.query)

    async def skill(args: SkillRead, context: ToolContext) -> dict:
        return await asyncio.to_thread(get_service().plugins.read, args)

    async def materialize(args: SkillMaterialize, context: ToolContext) -> dict:
        from chemistry_agent.hosting.native import current_native

        service = get_service()
        await scope(context)
        remote = current_native.get()
        if remote and remote.session == context.session_id:
            package, source = await asyncio.to_thread(service.plugins.locate, args.skill)
            directory = await remote.materialize_skill(args.skill, source)
            return {
                "directory": directory,
                "revision": package["revision"],
                "host": remote.config.host_id,
            }
        workspace = (
            await workspace_for_session(context.session_id)
            if workspace_for_session
            else service.workspaces / context.session_id
        )
        return await asyncio.to_thread(service.plugins.materialize, args.skill, workspace)

    def asset_handler(method):
        async def invoke(args, context):
            return await getattr(get_service().assets, method)(await scope(context), args)

        return invoke

    return [
        Tool(
            "knowledge_search",
            "Search project knowledge in OpenViking; read hits on demand.",
            KnowledgeQuery,
            search,
            True,
        ),
        Tool(
            "knowledge_read",
            "Read project knowledge by relative path, L0/L1/full content.",
            KnowledgeRead,
            read,
            True,
        ),
        Tool(
            "knowledge_write",
            "Save a project note in OpenViking; state evidence and applicability."
            " Stable relative path replaces the same note. Never store secrets.",
            KnowledgeWrite,
            write,
            True,
        ),
        Tool(
            "skill_list",
            "List enabled shared skill plugins; query filters name/description.",
            SkillQuery,
            skills,
            True,
        ),
        Tool(
            "skill_read",
            "Read SKILL.md or a bundled reference/script, with version and file list.",
            SkillRead,
            skill,
            True,
        ),
        Tool(
            "skill_materialize",
            "Copy a skill and its scripts into this agent workspace for use."
            " Returns its directory/version; does not execute or install dependencies.",
            SkillMaterialize,
            materialize,
            True,
        ),
        *(
            Tool(name, description, model, asset_handler(method), True)
            for name, (model, description, method) in ASSET_TOOLS.items()
        ),
    ]
