"""Local Chem Code test room with scripted speakers and real RDKit tools."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from chemistry_agent.api import create_app
from chemistry_agent.config import Settings

# Configuration
port = 18973

members = (
    ("Gemini", "拟定比较方案，需要计算时交给 Codex。"),
    ("Codex", "用 RDKit 检查证据，将结果送审并根据反馈修订。"),
    ("DeepSeek", "审查分子身份判断，缺少结构证据时退回 Codex。"),
)


def tool(name: str, **arguments: str) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": uuid4().hex,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def transfer(name: str, assignment: str) -> dict:
    return tool("group_handoff", target=name, task=assignment)


def answer(text: str) -> dict:
    return {"role": "assistant", "content": text}


def demo_reply(model: str, messages: list[dict]) -> dict:
    start = max(i for i, item in enumerate(messages) if item["role"] == "user")
    request, _, team = messages[start]["content"].partition("\n\n群组成员：")
    assignment = team.partition("当前交接任务：")[2].split("\n", 1)[0]
    results = [
        json.loads(item["content"]) for item in messages[start + 1 :] if item["role"] == "tool"
    ]
    if not team:
        return answer("这是协作试玩，请打开 /demo 进入预设群组。")
    if model == "Gemini":
        if "只要方案" in request or "只出方案" in request:
            return answer(
                "先比较 CCO 和 COC 的分子式及摩尔质量，再检查规范 SMILES。"
            )
        if not any(word in request for word in ("乙醇", "二甲醚", "CCO", "COC")):
            return answer(
                "可以测试「核对乙醇和二甲醚是不是同一种分子」或「只要方案」。"
            )
        return transfer("Codex", "比较乙醇 CCO 和二甲醚 COC 的分子式与摩尔质量，并把证据送审。")
    if model == "Codex":
        if "规范 SMILES" in assignment:
            if len(results) < 2:
                return tool("molecular_properties", smiles=("CCO", "COC")[len(results)])
            structures = [item["canonical_smiles"] for item in results]
            return transfer(
                "DeepSeek",
                f"已补证：规范 SMILES 分别为 {structures[0]} 和 {structures[1]}。请复核连接关系。",
            )
        if len(results) < 2:
            return tool("molecular_properties", smiles=("CCO", "COC")[len(results)])
        evidence = "、".join(
            f"{item['formula']} ({item['molecular_weight_g_mol']} g/mol)" for item in results
        )
        return transfer(
            "DeepSeek", f"第一版只汇总了分子式和质量：{evidence}。请判断身份结论的证据是否充分。"
        )
    if "第一版" in assignment:
        return transfer(
            "Codex", "分子式和质量不能区分构造异构体。请补充两个规范 SMILES 并重新送审。"
        )
    if "已补证" in assignment:
        return answer(
            f"复核通过。{assignment.split('。')[0]}。"
            "乙醇含 C–C–O 骨架，二甲醚为 C–O–C。二者是构造异构体。"
        )
    return answer(
        "仅凭分子式和摩尔质量不能区分构造异构体。还需核对连接关系，例如比较规范 SMILES。"
    )


def simulated_provider(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(200, json={"data": [{"id": name} for name, _ in members]})
    body = json.loads(request.content)
    message = demo_reply(body["model"], body["messages"])
    reason = "tool_calls" if message.get("tool_calls") else "stop"
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": reason}]})


async def seed_room(app: FastAPI) -> str:
    """Configure the same preset and group endpoints used by the browser."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/sessions", json={"title": "协作试玩 · 模拟模型 / RDKit 实算"}
        )
        session = response.raise_for_status().json()["id"]
        refs = []
        for name, instructions in members:
            preset_id = str(uuid4())
            result = await client.put(
                f"/api/agents/presets/{preset_id}",
                json={
                    "name": name,
                    "model": name,
                    "backend": "builtin",
                    "instance_id": "local-builtin",
                    "instructions": instructions,
                },
            )
            refs.append({"id": preset_id, "version": result.raise_for_status().json()["version"]})
        result = await client.put(
            f"/api/agents/sessions/{session}/group", json={"members": refs, "strategy": "handoff"}
        )
        result.raise_for_status()
        return session


def main() -> None:
    with TemporaryDirectory(prefix="chem-group-demo-") as directory:
        root = Path(directory)
        settings = Settings(
            _env_file=None,
            database_path=root / "demo.sqlite3",
            platform_path=root / "platform",
            deepseek_api_key="local-fixture",
            deepseek_base_url="https://fixture.invalid",
            deepseek_model="Gemini",
            context_window_tokens=131072,
            ssh_hosts="",
            auth_enabled=False,
            automation_enabled=False,
            platform_agent_enabled=False,
            research_memory_enabled=False,
            web_search_enabled=False,
        )
        app = create_app(settings, transport=httpx.MockTransport(simulated_provider))
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application: FastAPI) -> AsyncIterator[None]:
            async with original(application):
                application.state.demo_session = await seed_room(application)
                yield

        app.router.lifespan_context = lifespan

        @app.get("/demo", include_in_schema=False)
        async def room() -> RedirectResponse:
            return RedirectResponse(f"/?session={app.state.demo_session}")

        uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
