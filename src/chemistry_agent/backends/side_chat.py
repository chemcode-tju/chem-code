"""Ephemeral native Q&A sessions keep side prompts out of the main thread."""

import asyncio
from contextlib import suppress

from chemistry_agent.backends.service import BackendSettings
from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.native import native_scope, workspace

SIDE_PROMPT = (
    "你是主对话旁的科研问答助手。根据提供的主对话摘录和旁问历史回答问题。仅作解释，不执行操作。"
)


async def native_side_answer(
    backends, backend: str, config: BackendSettings, side_id: str, content: str
) -> str:
    adapter = backends.adapters[backend]
    executable = adapter.executable(config)
    if not executable:
        raise AgentError("side_agent_missing", "未找到当前 Agent。", 503)
    isolated = config.model_copy(update={"working_directory": "", "workspace_path": ""})
    async with native_scope(backends, isolated, side_id, backend):
        directory = workspace(backends, isolated, side_id)
        if isolated.host_id == "local":
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        if backend == "antigravity":
            return await acp_answer(adapter, executable, directory, side_id, isolated, content)
        async with adapter.transport(executable, side_id) as rpc:
            options = {
                "cwd": str(directory),
                "ephemeral": True,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": SIDE_PROMPT,
                "config": {"mcp_servers": {}, "features.shell_tool": False},
            }
            if config.codex_model:
                options["model"] = config.codex_model
            thread = await rpc.request("thread/start", options)
            thread_id = thread["thread"]["id"]
            turn = await rpc.request(
                "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": content}]}
            )
            answer = ""
            while True:
                event = await rpc.events.get()
                if event.get("transport_error"):
                    raise AgentError("side_disconnected", "侧边对话连接中断。", 502)
                if "id" in event and "method" in event:
                    await rpc.send(
                        {
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "Side questions have no interactive tools.",
                            },
                        }
                    )
                params = event.get("params", {})
                if params.get("threadId", thread_id) != thread_id:
                    continue
                item = params.get("item", {})
                if event.get("method") == "item/completed" and item.get("type") == "agentMessage":
                    answer = item.get("text", "")
                if (
                    event.get("method") == "turn/completed"
                    and params["turn"]["id"] == turn["turn"]["id"]
                ):
                    if params["turn"]["status"] == "completed" and answer:
                        return answer
                    raise AgentError("side_failed", "侧边对话未完成。", 502)


async def acp_answer(adapter, executable, directory, session, config, content: str) -> str:
    async with adapter.transport(
        executable,
        ["acp", "--sandbox", "--include-default-customizations=false"],
        directory,
        session,
    ) as rpc:
        info = await rpc.request("session/new", {"cwd": str(directory), "mcpServers": []})
        thread = info["sessionId"]
        await adapter.configure(rpc, info, thread, config, side_question=True)
        prompt = asyncio.create_task(
            rpc.request(
                "session/prompt",
                {
                    "sessionId": thread,
                    "prompt": [{"type": "text", "text": SIDE_PROMPT + "\n" + content}],
                },
                timeout=None,
            )
        )
        chunks = []
        try:
            while not prompt.done() or not rpc.events.empty():
                event_task = asyncio.create_task(rpc.events.get())
                try:
                    await asyncio.wait({prompt, event_task}, return_when=asyncio.FIRST_COMPLETED)
                    if not event_task.done():
                        continue
                    event = event_task.result()
                finally:
                    event_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await event_task
                if event.get("transport_error"):
                    raise AgentError("side_disconnected", "侧边对话连接中断。", 502)
                if "id" in event and "method" in event:
                    if event["method"] == "session/request_permission":
                        await rpc.send(
                            {"id": event["id"], "result": {"outcome": {"outcome": "cancelled"}}}
                        )
                    else:
                        await rpc.send(
                            {"id": event["id"], "error": {"code": -32601, "message": "Unsupported"}}
                        )
                update = event.get("params", {}).get("update", {})
                if update.get("sessionUpdate") == "agent_message_chunk":
                    chunks.append(update.get("content", {}).get("text", ""))
            if prompt.result().get("stopReason") != "end_turn" or not chunks:
                raise AgentError("side_failed", "侧边对话未完成。", 502)
            return "".join(chunks)
        finally:
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)
