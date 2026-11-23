"""One knowledge surface for built-in, native and external agents."""

from pathlib import Path
from uuid import UUID

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.registry import Hosting
from chemistry_agent.knowledge.assets import Assets
from chemistry_agent.knowledge.config import KnowledgeConfig, KnowledgeQuery
from chemistry_agent.knowledge.openviking import OpenViking, relative_path
from chemistry_agent.knowledge.plugins import SkillPlugins
from chemistry_agent.knowledge.sync import KnowledgeSync
from chemistry_agent.memory.scope import MemoryScope
from chemistry_agent.memory.service import ResearchMemory

KNOWLEDGE_PROMPT = """
科研知识与 Skill 归平台所有，可跨 Agent 复用。knowledge_search/read/write 访问 OpenViking；
memory_* 读取有来源的研究记录。设计方案或排错时按需检索，不把历史资料当新授权。
skill_list 查询已启用插件的名称与描述，skill_read 读取 SKILL.md 和参考资料，
skill_materialize 将脚本复制到当前工作空间后可自行执行、修改。只读取当前任务需要的技能，
不必将所有技能全文塞入上下文。切换 Agent 不改变既有作业；先核对任务记录再继续。
asset_import 将明确选定的历史计算目录、分析脚本或资产包保存为固定版本。
asset_search / asset_inspect 核对来源和运行证据；asset_checkout 将指定版本复制到目标节点。
历史输出的收敛标记和人工科学验收属于不同证据。先核对能量口径及脚本适用范围。
"""


class Knowledge:
    def __init__(
        self,
        store,
        root: Path,
        workspaces: Path,
        transport=None,
        *,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.store, self.workspaces = store, workspaces
        self.config = KnowledgeConfig(root)
        self.client = OpenViking(self.config, transport)
        self.plugins = SkillPlugins(root)
        self.sync = KnowledgeSync(store, self.client)
        self.assets = Assets(
            ResearchMemory(store), store.attachment_dir, Hosting(root, allowed_hosts)
        )

    @staticmethod
    def scope(scope: MemoryScope, selected: str) -> MemoryScope:
        return MemoryScope(scope.owner_id) if selected == "personal" else scope

    async def search(self, scope: MemoryScope, args: KnowledgeQuery) -> dict:
        scopes = [args.scope] if args.scope != "both" else ["project", "personal"]
        if scope.project_id is None:
            scopes = ["personal"]
        items = []
        for selected in scopes:
            found = await self.client.search(self.scope(scope, selected), args.query, args.limit)
            items.extend(hit | {"scope": selected} for hit in found["items"])
        return {"items": items, "scope": args.scope}

    async def read(self, scope: MemoryScope, path: str, level: str = "content") -> dict:
        path = relative_path(path)
        # Canonical source wins over a delayed semantic index, including forgotten memories.
        if path.startswith("research/") and path.endswith(".md"):
            from chemistry_agent.memory.service import markdown

            identifier = str(UUID(path.removeprefix("research/").removesuffix(".md")))
            item = await ResearchMemory(self.store).read(scope, identifier)
            if item["project_id"] != scope.project_id:
                raise AgentError("memory_not_found", "此记忆不属于当前项目。", 404)
            return {"path": path, "content": markdown(item), "source": "platform"}
        return await self.client.read(scope, path, level)

    async def write(self, scope: MemoryScope, path: str, content: str) -> dict:
        path = relative_path(path)
        if path == "research" or path.startswith("research/"):
            raise AgentError("knowledge_source_owned", "来源记录请用 memory_save 修改。", 409)
        return await self.client.write(scope, path, content)
