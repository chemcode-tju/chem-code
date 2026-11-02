"""Drive one ephemeral, read-only Codex app-server turn over stdio."""

import asyncio
import json
from pathlib import Path

from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.live import StructuredPreview
from chemistry_agent.schemas import TokenUsage


async def read_codex_turn(
    proc: asyncio.subprocess.Process, cwd: Path, model: str, prompt: str, schema: dict
) -> tuple[str, TokenUsage]:
    async def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def read() -> dict:
        raw = await proc.stdout.readline()
        if not raw:
            raise ValueError("unexpected end of app-server stream")
        return json.loads(raw)

    async def request(identifier: int, method: str, params: dict) -> dict:
        await send({"id": identifier, "method": method, "params": params})
        while True:
            item = await read()
            if item.get("id") == identifier:
                return item["result"]

    await request(
        1,
        "initialize",
        {"clientInfo": {"name": "chemistry_agent", "title": "Chemistry Agent", "version": "0.1.0"}},
    )
    await send({"method": "initialized"})
    thread = await request(
        2,
        "thread/start",
        {
            "cwd": str(cwd),
            "model": model or None,
            "ephemeral": True,
            "sandbox": "read-only",
            "approvalPolicy": "never",
        },
    )
    tid = thread["thread"]["id"]
    await send(
        {
            "id": 3,
            "method": "turn/start",
            "params": {
                "threadId": tid,
                "input": [{"type": "text", "text": prompt}],
                "outputSchema": schema,
            },
        }
    )
    preview = StructuredPreview()
    usage = TokenUsage()
    final = ""
    size = 0
    while True:
        item = await read()
        if "error" in item and "id" in item:
            raise ValueError("app-server rejected request")
        params = item.get("params") or {}
        if params.get("threadId") != tid:
            continue
        method = item.get("method")
        if method == "item/agentMessage/delta":
            delta = params["delta"]
            size += len(delta)
            if size > 16 * 1024 * 1024:
                raise ValueError("oversized response")
            preview.feed(delta)
        elif method == "item/completed" and params["item"]["type"] == "agentMessage":
            final = params["item"]["text"]
        elif method == "thread/tokenUsage/updated":
            total = params["tokenUsage"]["total"]
            usage = TokenUsage(
                prompt_tokens=total["inputTokens"],
                completion_tokens=total["outputTokens"],
                total_tokens=total["totalTokens"],
            )
        elif method == "turn/completed":
            if params["turn"]["status"] != "completed" or not final:
                raise AgentError("chatgpt_response_failed", "ChatGPT 未完成本轮回答。", 502)
            return final, usage
