"""Reassemble Chat Completions deltas before validating or executing any tools."""

import json

import httpx

from chemistry_agent.errors import AgentError
from chemistry_agent.runtime.live import LiveAnswer


async def read_chat_stream(response: httpx.Response, preview: LiveAnswer) -> dict:
    parts: dict[int, dict] = {}
    text = reasoning = ""
    finish = None
    usage = {}
    size = 0
    async for line in response.aiter_lines():
        size += len(line)
        if size > 16 * 1024 * 1024:
            raise ValueError("stream exceeded limit")
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            if not finish:
                break
            return {
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {
                            "role": "assistant",
                            "content": text,
                            "reasoning_content": reasoning or None,
                            "tool_calls": [parts[i] for i in sorted(parts)],
                        },
                    }
                ],
                "usage": usage,
            }
        event = json.loads(data)
        if event.get("error"):
            raise AgentError("provider_error", "模型服务在生成时返回错误。", 502)
        usage = event.get("usage") or usage
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if content := delta.get("content"):
                text += content
                preview.set_text(text)
            reasoning += delta.get("reasoning_content") or ""
            for call in delta.get("tool_calls") or []:
                index = call["index"]
                if type(index) is not int or not 0 <= index < 16:
                    raise ValueError("invalid tool index")
                part = parts.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                part["id"] += call.get("id") or ""
                for key in ("name", "arguments"):
                    part["function"][key] += (call.get("function") or {}).get(key) or ""
            finish = choice.get("finish_reason") or finish
    raise AgentError("provider_stream_interrupted", "模型响应流中断，未收到完成确认。", 502)
