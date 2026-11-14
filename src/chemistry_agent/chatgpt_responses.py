"""ChatGPT plan inference on the documented public Responses API (HTTP/SSE)."""

import asyncio
import json
from typing import Any

import httpx

from chemistry_agent.chatgpt_plan import RESOURCE, ChatGPTPlan
from chemistry_agent.errors import AgentError, retry_after_seconds
from chemistry_agent.provider import AssistantMessage, Completion
from chemistry_agent.runtime.live import StructuredPreview
from chemistry_agent.schemas import TokenUsage


def model_window(item: dict) -> int | None:
    """Use the advertised normal window, never an experimental maximum."""
    window = item.get("context_window")
    percent = item.get("effective_context_window_percent", 95)
    if type(window) is int and 8192 <= window <= 16_000_000:
        if type(percent) is int and 1 <= percent <= 100:
            return window * percent // 100
    return None


class PlanResponses:
    def __init__(self, authorization: ChatGPTPlan):
        self.authorization = authorization

    async def list_models(self, user_id: str | None) -> list[dict]:
        token = await self.authorization.access_token(user_id)
        try:
            async with httpx.AsyncClient(
                transport=self.authorization.transport, timeout=30
            ) as client:
                response = await client.get(
                    f"{RESOURCE}/models", headers={"Authorization": f"Bearer {token}"}
                )
            if response.status_code != 200:
                raise AgentError(
                    "model_catalog_failed", "无法读取 ChatGPT 模型列表，请检查授权及额度。", 502
                )
            return [
                {
                    "id": item["slug"],
                    "name": item.get("display_name") or item["slug"],
                    **(
                        {
                            "efforts": [
                                e.get("effort", e.get("reasoningEffort"))
                                for e in item["supported_reasoning_levels"]
                                if isinstance(e, dict)
                            ]
                        }
                        if isinstance(item.get("supported_reasoning_levels"), list)
                        and item["supported_reasoning_levels"]
                        else {}
                    ),
                    **({"window": window} if (window := model_window(item)) else {}),
                }
                for item in response.json()["models"]
                if item.get("visibility") == "list" and isinstance(item.get("slug"), str)
            ]
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise AgentError(
                "model_catalog_failed", "ChatGPT 模型列表暂时不可用，请稍后刷新。", 502
            ) from exc

    async def complete(
        self,
        user_id: str | None,
        model: str,
        messages: list[dict],
        tools: list[dict],
        *,
        allow_tools: bool,
        max_tokens: int | None = None,
        effort: str = "",
        **_: Any,
    ) -> Completion:
        from chemistry_agent.chatgpt import response_schema

        if not model:
            catalog = await self.list_models(user_id)
            if not catalog:
                raise AgentError("model_catalog_empty", "当前 ChatGPT 账号没有可选择的模型。", 503)
            model = catalog[0]["id"]
        token = await self.authorization.access_token(user_id)
        instructions = (
            "You are Chem Code's model backend. Follow the system messages in the supplied "
            "conversation. Return a JSON assistant message matching the schema. "
            "Do not execute tools. "
            "Request only listed functions with unique call IDs and JSON-encoded arguments; "
            "Chem Code executes them and provides results on the next request. "
            "Never claim that a calculation succeeded without a supplied result. "
            + (
                f"Aim for at most {max_tokens} response tokens."
                if max_tokens is not None
                else "Keep prose concise; include complete code and tool arguments. "
                "Do not truncate executable plans to shorten the response."
            )
        )
        body = {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"messages": messages, "tools": tools if allow_tools else []},
                        ensure_ascii=False,
                    ),
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "chemistry_assistant",
                    "strict": True,
                    "schema": response_schema(),
                }
            },
        }
        if effort:
            body["reasoning"] = {"effort": effort}
        try:
            async with asyncio.timeout(self.authorization.store.settings.codex_timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self.authorization.transport, timeout=60
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{RESOURCE}/responses",
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as response:
                        if response.status_code == 429 or response.status_code >= 500:
                            raise AgentError(
                                "provider_rate_limited"
                                if response.status_code == 429
                                else "provider_unavailable",
                                "ChatGPT 暂时受限，请等待服务恢复。",
                                503,
                                retry_after=retry_after_seconds(
                                    response.headers.get("retry-after")
                                ),
                            )
                        if response.status_code != 200:
                            raise AgentError(
                                "chatgpt_response_failed",
                                f"ChatGPT 请求失败（HTTP {response.status_code}），"
                                "请检查授权、模型和用量。",
                                502,
                            )
                        completed = await read_completed(response)
            texts = [
                part["text"]
                for item in completed.get("output", [])
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ]
            message = AssistantMessage.model_validate_json("".join(texts))
            names = {tool["function"]["name"] for tool in tools} if allow_tools else set()
            ids = [call.id for call in message.tool_calls]
            if (
                len(ids) != len(set(ids))
                or any(call.function.name not in names for call in message.tool_calls)
                or (not message.tool_calls and not (message.content or "").strip())
            ):
                raise ValueError("Invalid tool plan")
            usage = completed.get("usage") or {}
            prompt, completion = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            return Completion(
                message=message,
                usage=TokenUsage(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=prompt + completion,
                ),
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise AgentError("provider_timeout", "ChatGPT 响应超时，请稍后重试。", 504) from exc
        except httpx.RequestError as exc:
            raise AgentError("provider_unreachable", "与 ChatGPT 的连接中断。", 502) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError(
                "provider_invalid_response", "ChatGPT 返回了无法解析的响应。", 502
            ) from exc


async def read_completed(response: httpx.Response) -> dict:
    """Only a completed terminal event proves success; partial text is not a result."""
    preview = StructuredPreview()
    completed_items: dict[int, dict] = {}
    data: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        size += len(line)
        if size > 16 * 1024 * 1024:
            raise ValueError("Response stream exceeded limit")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            event = json.loads("\n".join(data))
            data.clear()
            kind = event.get("type")
            if kind == "response.output_text.delta":
                preview.feed(event["delta"])
            if kind == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "message" and item.get("role") == "assistant":
                    completed_items[event["output_index"]] = item
            if kind == "response.completed":
                result = event["response"]
                if result.get("status") != "completed":
                    raise ValueError("Missing completed status")
                if not result.get("output"):
                    result["output"] = [completed_items[i] for i in sorted(completed_items)]
                return result
            if kind in {"response.failed", "response.incomplete", "error"}:
                error = event.get("response", {}).get("error") or event.get("error") or {}
                code = error.get("code") if isinstance(error, dict) else None
                message = "ChatGPT 响应未完成，请检查模型权限与用量后重试。"
                if code in {
                    "subscription_sharing_usage_limit_exceeded",
                    "subscription_sharing_usage_unavailable",
                }:
                    message = "ChatGPT 套餐用量不可用或已达上限，请在 ChatGPT 设置的用量页面检查。"
                    raise AgentError("provider_rate_limited", message, 503)
                raise AgentError("chatgpt_response_failed", message, 502)
    raise AgentError("chatgpt_stream_interrupted", "ChatGPT 响应流中断，未收到完成确认。", 502)
