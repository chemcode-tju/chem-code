"""Expose local settings operations through the same workers used by the settings page."""

import re
from contextvars import ContextVar
from uuid import UUID

from chemistry_agent.automation.configuration import ExecutionTarget, PotentialPath
from chemistry_agent.automation.models import CodeCreate, StrictModel
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext

configuration_grant: ContextVar[bool] = ContextVar("configuration_grant", default=False)


_OBJECT = (
    r"(?:计算软件|计算程序|软件|程序|环境脚本|服务器|集群|赝势(?:源|库|目录|路径)?|执行目标|队列|资源|"
    r"执行环境|默认(?:执行目标|环境|队列|资源)|本地\s*AiiDA|AiiDA|VASP|GROMACS|QE|"
    r"server|cluster|software|potential source|execution target|queue|daemon)"
)
_ACTION = (
    r"(?:注册|登记|添加|保存|配置|设置|修改|更新|初始化|启动|关联|绑定|录入|"
    r"register|add|save|configure|initialize|start|update)"
)
_SCOPE = r"(?:(?:在|为)\s*[A-Za-z0-9_.-]+\s*(?:服务器)?\s*(?:上)?\s*)?"
_REQUEST = re.compile(
    rf"^(?:(?:你|您|请|麻烦|现在|帮我|替我|直接|我要|我想|我需要|please)\s*)*{_SCOPE}"
    rf"(?:{_ACTION}\s*(?:(?:一个|一下|新的|a |an |new )\s*)*{_OBJECT}|"
    rf"(?:把|将)\s*(?:[A-Za-z0-9_.-]+\s*(?:服务器)?(?:上)?的\s*)?"
    rf"{_OBJECT}[^，。？！\n]{{0,40}}(?:设为|改为|设置为|改成|更新为|关联到))",
    re.IGNORECASE,
)
_POLITE = re.compile(
    r"^(?:请问)?(?:你|您)?(?:能不能|可以|能|可否)(?=帮我|替我)|^(?:can|could) you\s+", re.I
)
_NON_REQUEST = re.compile(
    r"[？?]|吗(?:[。！\s]|$)|之前|先别|是什么|有什么|的步骤|失败了|"
    r"^(?:>|引[用述]|示例)|如果|假设|假如|比如|例如|能否|是否|怎么|如何|不要|不用|先不|别(?:帮|注册|设置|改)|"
    r"不需要(?:你|实际|注册|执行)|仅(?:讨论|说明)|只(?:讨论|说明|查看)|"
    r"\b(?:if|example|hypothetically|do not|don't|how to|only explain)\b",
    re.IGNORECASE | re.MULTILINE,
)


def authorizes_configuration(message: str) -> bool:
    """Recognize an imperative in the user's own opening line, never in tool/attachment content."""
    text = message.strip()
    header = text.partition("\n")[0].strip()
    if header == "/configure":
        return not _NON_REQUEST.search(text[len(header) :])
    # “能帮我添加服务器吗？” is a request; “怎么添加服务器？” is a question.
    if _POLITE.match(header):
        text = _POLITE.sub("", text, count=1)
        text = re.sub(r"(?:好吗|吗)?[?？]?$", "", text).strip()
        header = text.partition("\n")[0].strip()
    return bool(_REQUEST.match(header) and not _NON_REQUEST.search(text))


class SettingsQuery(StrictModel):
    server_id: UUID | None = None


class QueueQuery(StrictModel):
    server_id: UUID


def settings_tools(service: AutomationService) -> list[Tool]:
    def tool(
        name: str, description: str, model: type[StrictModel], action: str, *, write: bool = False
    ) -> Tool:
        async def handle(body: StrictModel, context: ToolContext) -> dict:
            if write and not configuration_grant.get():
                raise AgentError(
                    "settings_grant_required",
                    "请在本轮直接说明需要登记或更改的设置，也可用 /configure。",
                    403,
                )
            return await service.call(action, body.model_dump(mode="json", exclude_none=True))

        if write:
            description += " Requires an explicit configuration request in the current user turn."
        return Tool(name, description, model, handle, True)

    return [
        tool(
            "calculation_settings",
            "Check local AiiDA readiness and configuration gaps, optionally scoped to a server.",
            SettingsQuery,
            "settings_status",
        ),
        tool(
            "calculation_initialize",
            "Create the local profile if missing and start its daemon.",
            StrictModel,
            "settings_initialize",
            write=True,
        ),
        tool(
            "calculation_register_software",
            "Register a remote software name, version, executable and optional environment script.",
            CodeCreate,
            "create_code",
            write=True,
        ),
        tool(
            "calculation_register_potential_source",
            "Register a remote PBE library directory for on-demand imports.",
            PotentialPath,
            "register_potential_path",
            write=True,
        ),
        tool(
            "calculation_save_execution_target",
            "Save queue/resource defaults for VASP, without scientific parameters.",
            ExecutionTarget,
            "save_execution_target",
            write=True,
        ),
        tool(
            "calculation_server_queues",
            "Read advertised queue and GPU options for a managed server.",
            QueueQuery,
            "scheduler_options",
        ),
    ]
