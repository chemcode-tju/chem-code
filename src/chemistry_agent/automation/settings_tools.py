"""Recognize explicit local configuration requests for the current platform."""

import re
from contextvars import ContextVar

configuration_grant: ContextVar[bool] = ContextVar("configuration_grant", default=False)


_OBJECT = (
    r"(?:计算软件|计算程序|软件|程序|环境脚本|服务器|集群|赝势(?:源|库|目录|路径)?|执行目标|队列|资源|"
    r"执行环境|默认(?:执行目标|环境|队列|资源)|VASP|GROMACS|QE|"
    r"server|cluster|software|potential source|execution target|queue)"
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
