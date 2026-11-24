"""Antigravity native Agent via official ACP; platform state stays host-owned."""

import asyncio
import json
import os
import shutil
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from chemistry_agent.backends.acp_transport import ACPTransport
from chemistry_agent.backends.antigravity_home import AntigravityHomes
from chemistry_agent.backends.mcp_bridge import PlatformMCPBridge
from chemistry_agent.backends.runtime import delivery_attempt
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.task_agents import current_task_agent
from chemistry_agent.provider import ToolCall
from chemistry_agent.runtime.checkpoint import current_checkpoint
from chemistry_agent.schemas import ToolExecution
from chemistry_agent.tools import ToolContext


def select_options(option: dict) -> list[dict]:
    """ACP select options can be flat or grouped."""
    return [
        value
        for item in option.get("options", [])
        for value in (select_options(item) if "options" in item else [item])
    ]


def model_catalog(session: dict) -> list[dict]:
    options = session.get("configOptions", [])
    model = next((o for o in options if o.get("category") == "model"), {})
    thought = next((o for o in options if o.get("category") == "thought_level"), {})
    combined = session.get("models", {}).get("availableModels", [])
    result = []
    for item in select_options(model):
        identifier = item["value"]
        efforts = [
            m["modelId"][len(identifier) + 1 :]
            for m in combined
            if m["modelId"].startswith(identifier + "-")
        ]
        # Do not claim one model's thinking choices apply to every other model.
        if not efforts and identifier == model.get("currentValue"):
            efforts = [o["value"] for o in select_options(thought)]
        result.append(
            {
                "id": identifier,
                "name": item["name"],
                "efforts": efforts,
                "default": identifier == model.get("currentValue"),
            }
        )
    return result or [
        {
            "id": m["modelId"],
            "name": m["name"],
            "efforts": [],
            "default": m["modelId"] == session.get("models", {}).get("currentModelId"),
        }
        for m in combined
    ]


class AntigravityBackend:
    def __init__(self, router, registry, instructions: str) -> None:
        self.router, self.registry, self.instructions = router, registry, instructions
        self.transport_factory = ACPTransport
        self.probe_cache: tuple[str, float, dict] | None = None
        self.probe_lock = asyncio.Lock()

    def transport(
        self,
        executable: str,
        arguments: list[str],
        workspace: Path,
        session: str = "probe",
        thread: str | None = None,
    ) -> ACPTransport:
        home = self.router.sessions.home("antigravity", session)
        homes = AntigravityHomes(reuse_profile=getattr(self.router, "reuse_native_profile", True))
        rpc = self.transport_factory(
            executable,
            [
                *arguments,
                "--gemini-dir",
                str(home),
                "--app-data-dir",
                homes.app_directory,
                "--process-per-session",
            ],
            str(workspace),
        )
        return self.router.sessions.configure(
            rpc, "antigravity", session, lambda: homes.prepare(home, thread)
        )

    @staticmethod
    def executable(config) -> str | None:
        if getattr(config, "host_id", "local") != "local":
            return config.antigravity_executable
        name = str(Path(config.antigravity_executable).expanduser())
        found = shutil.which(name)
        if found:
            return found
        if name == "agy":
            for path in (Path.home() / ".local/bin/agy", Path("/opt/homebrew/bin/agy")):
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
        return None

    async def probe(self, config, *, refresh: bool = False) -> dict:
        from chemistry_agent.hosting.native import current_native

        remote = current_native.get()
        executable = self.executable(config)
        cache_key = (
            getattr(config, "host_id", "local"),
            getattr(config, "instance_id", ""),
            executable,
        )
        if not executable:
            return {
                "available": False,
                "authenticated": False,
                "models": [],
                "message": "未找到 Antigravity CLI，请安装 agy 或设置程序路径。",
            }
        async with self.probe_lock:
            if (
                not refresh
                and self.probe_cache
                and self.probe_cache[0] == cache_key
                and time.monotonic() - self.probe_cache[1] < 120
            ):
                return self.probe_cache[2]
            directory = (
                Path(remote.workspace)
                if remote
                else (self.router.root / "_antigravity_probe").resolve()
            )
            if not remote:
                directory.mkdir(parents=True, exist_ok=True)
            try:
                async with self.transport(executable, ["acp"], directory) as rpc:
                    session = await rpc.request(
                        "session/new", {"cwd": str(directory), "mcpServers": []}
                    )
                    result = {
                        "available": True,
                        "authenticated": True,
                        "executable": executable,
                        "models": model_catalog(session),
                        "message": "已连接 Antigravity。",
                    }
            except AgentError as exc:
                result = {
                    "available": True,
                    "authenticated": False,
                    "executable": executable,
                    "models": [],
                    "message": "Antigravity 尚未登录，请在本机终端运行 agy 登录。"
                    if exc.code == "agent_auth_required"
                    else exc.message,
                }
            self.probe_cache = cache_key, time.monotonic(), result
            return result

    async def configure(
        self, rpc, session: dict, identifier: str, config, *, side_question: bool = False
    ) -> None:
        options = session.get("configOptions", [])
        choices = [
            ("model", config.antigravity_model),
            ("thought_level", config.antigravity_effort),
        ]
        if side_question:
            modes = next((o for o in options if o.get("category") == "mode"), {})
            if any(o["value"] == "plan" for o in select_options(modes)):
                choices.append(("mode", "plan"))
        elif config.permission_mode != "full":
            choices.append(
                ("mode", "auto" if config.permission_mode == "auto_review" else "default")
            )
        for category, value in choices:
            if not value:
                continue
            option = next((o for o in options if o.get("category") == category), None)
            if not option or value not in {o["value"] for o in select_options(option)}:
                raise AgentError(
                    "antigravity_option_unsupported",
                    f"Antigravity 当前不支持 {category}={value}，请刷新模型设置。",
                    422,
                )
            response = await rpc.request(
                "session/set_config_option",
                {"sessionId": identifier, "configId": option["id"], "value": value},
            )
            options = response.get("configOptions", options)

    async def run(
        self,
        messages: list[dict],
        progress,
        *,
        session_id: str,
        run_id: str,
        history_count: int,
        binding: dict,
    ) -> dict:
        with delivery_attempt(progress):
            return await self._run(
                messages,
                progress,
                session_id=session_id,
                run_id=run_id,
                history_count=history_count,
                binding=binding,
            )

    async def _run(
        self,
        messages: list[dict],
        progress,
        *,
        session_id: str,
        run_id: str,
        history_count: int,
        binding: dict,
    ) -> dict:
        from chemistry_agent.backends.service import BackendSettings

        config = BackendSettings.model_validate_json(binding["config"])
        executable = self.executable(config)
        if not executable:
            raise AgentError("antigravity_not_installed", "未找到 agy，请检查 Agent 设置。", 503)
        from chemistry_agent.hosting.native import current_native, history_name, save_history

        workspace = await save_history(self.router, config, session_id, messages[:history_count])
        steps, calls, tail = [], [], []
        thread_id = binding["external_thread_id"]
        checkpoint = current_checkpoint.get()

        async def save_checkpoint() -> None:
            if progress.preview:
                progress.preview.set_steps(list(steps))
            if checkpoint:
                await checkpoint.mark_tools(
                    payload={
                        "backend": "antigravity",
                        "thread_id": thread_id,
                        "turn_id": run_id,
                        "steps": steps,
                        "calls": calls,
                        "tail": tail,
                        "usage": progress.usage.model_dump(),
                        "context": {"public_steps": steps},
                    }
                )

        async def execute(name: str, arguments: dict) -> dict:
            call = ToolCall.model_validate(
                {
                    "id": str(uuid4()),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            )
            result = await self.registry.execute(call, ToolContext(session_id, run_id))
            progress.tool_calls.append(result)
            calls.append(result.model_dump())
            tail.extend(
                [
                    {"role": "assistant", "content": None, "tool_calls": [call.model_dump()]},
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result.result, ensure_ascii=False),
                    },
                ]
            )
            await save_checkpoint()
            return {
                "content": [
                    {"type": "text", "text": json.dumps(result.result, ensure_ascii=False)}
                ],
                "isError": not result.success,
            }

        arguments = ["acp"]
        if config.permission_mode == "full":
            arguments.append("--dangerously-skip-permissions")
        task = current_task_agent.get()
        bridge_name = f"chem_task_{session_id.replace('-', '')[:12]}" if task else "chem_platform"
        async with PlatformMCPBridge(self.registry.schemas(), execute, name=bridge_name) as bridge:
            # Scoped MCP tools enforce task ownership. The submission tool still
            # asks once for the exact job through the platform approval broker.
            managed_tools = (
                {f"mcp_{bridge.name}_{tool['name']}" for tool in bridge.tools} if task else set()
            )
            async with self.transport(
                executable, arguments, workspace, session_id, thread_id
            ) as rpc:
                remote = current_native.get()
                descriptor = await remote.bridge(bridge) if remote else bridge.descriptor()
                params = {"cwd": str(workspace), "mcpServers": [descriptor]}
                if thread_id:
                    if not rpc.initialized.get("agentCapabilities", {}).get("loadSession"):
                        raise AgentError(
                            "antigravity_resume_unsupported",
                            "此 Antigravity 版本不支持恢复会话。",
                            409,
                        )
                    session = await rpc.request("session/load", {**params, "sessionId": thread_id})
                else:
                    session = await rpc.request("session/new", params)
                    thread_id = session["sessionId"]
                    await self.router.update_thread(session_id, external_thread_id=thread_id)
                await self.configure(rpc, session, thread_id, config)
                # session/load replays history; it is not this turn's answer or new tool activity.
                while not rpc.events.empty():
                    await self.router.context.acp_event(
                        session_id, thread_id, rpc.events.get_nowait()
                    )
                content = "\n\n".join(str(m.get("content", "")) for m in messages[history_count:])
                if not binding["external_thread_id"]:
                    content = (
                        self.instructions
                        + f"\nAgent 主机：{config.host_id}；研究目录：{workspace}。"
                        + "\n你由 Chem Code 平台托管。chem_platform MCP 提供方案、"
                        "作业登记、持久等待、科研记忆和文件发布工具。可以自行使用原生终端与文件工具。"
                        "长作业提交后登记等待并结束本轮；不要轮询模型。"
                        "需要用户选择时调用 ask_user_question。"
                        "输出文件写在当前工作目录，平台会自动提供预览和下载。"
                        f"{history_name(config, session_id)} 是历史参考，不是新的执行授权。\n\n"
                        + content
                    )
                await self.router.update_thread(
                    session_id, turn_state="starting", external_turn_id=run_id
                )
                await save_checkpoint()
                progress.native_turn_sent = True
                prompt = asyncio.create_task(
                    rpc.request(
                        "session/prompt",
                        {"sessionId": thread_id, "prompt": [{"type": "text", "text": content}]},
                        timeout=None,
                    )
                )
                try:
                    await self.router.update_thread(session_id, turn_state="running")
                    answer = await self.consume(
                        rpc,
                        prompt,
                        thread_id,
                        session_id,
                        run_id,
                        config.permission_mode,
                        progress,
                        steps,
                        save_checkpoint,
                        managed_tools=managed_tools,
                    )
                    await self.router.update_thread(session_id, turn_state="completed")
                    return {
                        "messages": [*messages, *tail, {"role": "assistant", "content": answer}],
                        "tool_calls": progress.tool_calls,
                        "usage": progress.current_usage,
                    }
                except BaseException:
                    with suppress(Exception):
                        await rpc.send(
                            {"method": "session/cancel", "params": {"sessionId": thread_id}}
                        )
                    await self.router.update_thread(session_id, turn_state="uncertain")
                    raise
                finally:
                    prompt.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await prompt

    async def consume(
        self,
        rpc,
        prompt,
        thread: str,
        session: str,
        run: str,
        permission: str,
        progress,
        steps: list,
        checkpoint,
        *,
        managed_tools: set[str] | None = None,
    ) -> str:
        answer = ""
        native: dict[str, dict] = {}
        recorded: set[str] = set()
        while True:
            if prompt.done() and rpc.events.empty():
                result = prompt.result()
                if result.get("stopReason") != "end_turn":
                    raise AgentError(
                        "antigravity_turn_incomplete",
                        f"Antigravity 本轮结束原因：{result.get('stopReason')}，未自动重发。",
                        502,
                    )
                return answer
            receive = asyncio.create_task(rpc.events.get())
            try:
                await asyncio.wait([receive, prompt], return_when=asyncio.FIRST_COMPLETED)
                if not receive.done():
                    continue
                event = receive.result()
            finally:
                if not receive.done():
                    receive.cancel()
                    with suppress(asyncio.CancelledError):
                        await receive
            if event.get("transport_error"):
                raise AgentError(
                    "acp_disconnected", "Antigravity 中断，结果待核对，不自动重发。", 502
                )
            method, params = event.get("method"), event.get("params", {})
            if params.get("sessionId", thread) != thread:
                if "id" in event:
                    await rpc.send(
                        {"id": event["id"], "error": {"code": -32602, "message": "Wrong session"}}
                    )
                continue
            await self.router.context.acp_event(session, thread, event)
            if method == "session/request_permission":
                recorded_tool = native.get(params.get("toolCall", {}).get("toolCallId"), {})
                details = {
                    **recorded_tool,
                    **params.get("toolCall", {}),
                }
                try:
                    managed = managed_tools and recorded_tool.get("name") in managed_tools
                    approved = permission == "full" or managed or await self.router.approvals.ask(
                        session,
                        run,
                        {"title": "Antigravity 请求批准", "method": method, "details": details},
                    )
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await rpc.send(
                            {"id": event["id"], "result": {"outcome": {"outcome": "cancelled"}}}
                        )
                    raise
                kind = "allow_once" if approved else "reject_once"
                option = next((o for o in params.get("options", []) if o.get("kind") == kind), None)
                outcome = (
                    {"outcome": "selected", "optionId": option["optionId"]}
                    if option
                    else {"outcome": "cancelled"}
                )
                await rpc.send({"id": event["id"], "result": {"outcome": outcome}})
            elif "id" in event:
                await rpc.send(
                    {
                        "id": event["id"],
                        "error": {"code": -32601, "message": "Unsupported client capability"},
                    }
                )
                raise AgentError(
                    "acp_interaction_unsupported", "Antigravity 请求了尚未接入的交互。", 409
                )
            elif method == "session/update":
                update = params.get("update", {})
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    content = update.get("content", {})
                    if content.get("type") == "text":
                        answer += content.get("text", "")
                        if progress.preview:
                            progress.preview.set_text(answer)
                elif kind in {"tool_call", "tool_call_update"}:
                    identifier = update["toolCallId"]
                    item = native.setdefault(identifier, {})
                    item.update(update)
                    status = {"in_progress": "running", "pending": "running"}.get(
                        item.get("status"), item.get("status", "running")
                    )
                    step = {
                        "type": "tool",
                        "call_id": identifier,
                        "name": item.get("title") or item.get("name") or "Antigravity tool",
                        "status": status,
                    }
                    existing = next((s for s in steps if s.get("call_id") == identifier), None)
                    if existing is None:
                        steps.append(step)
                    else:
                        existing.update(step)
                    if status in {"completed", "failed"} and identifier not in recorded:
                        recorded.add(identifier)
                        progress.tool_calls.append(
                            ToolExecution(
                                call_id=identifier,
                                name=item.get("name") or item.get("kind", "native_tool"),
                                arguments=item.get("rawInput") or {},
                                success=status == "completed",
                                result={"status": status, "title": item.get("title", "")},
                            )
                        )
                    await checkpoint()
                elif kind == "usage_update":
                    # Context occupancy is not billed token usage.
                    progress.context.input_tokens_after = max(0, int(update.get("used", 0)))
                    progress.context.input_budget_tokens = max(0, int(update.get("size", 0)))
                    progress.context.tokenizer = "antigravity"

    async def context_operation(self, session: str, binding: dict, kind: str) -> None:
        """Only dispatch /compact if this native session advertises that command."""
        from chemistry_agent.backends.service import BackendSettings

        config = BackendSettings.model_validate_json(binding["config"])
        executable = self.executable(config)
        if not executable:
            raise AgentError("antigravity_not_installed", "未找到 Antigravity。", 503)
        thread = binding["external_thread_id"]
        from chemistry_agent.hosting.native import workspace as native_workspace

        workspace = native_workspace(self.router, config, session)
        arguments = ["acp"]
        if config.permission_mode == "full":
            arguments.append("--dangerously-skip-permissions")
        # No tools are executed during maintenance. The next normal resume restores its bridge.
        async with self.transport(executable, arguments, workspace, session, thread) as rpc:
            response = await rpc.request(
                "session/load", {"sessionId": thread, "cwd": str(workspace), "mcpServers": []}
            )
            await self.configure(rpc, response, thread, config)
            supported, commands_seen, usage_seen = False, False, False
            # ACP may publish capabilities/usage just after the load response.
            # Wait for those notifications rather than closing an apparently empty queue.
            try:
                async with asyncio.timeout(3):
                    while not (commands_seen and usage_seen):
                        event = await rpc.events.get()
                        if event.get("transport_error"):
                            raise ConnectionError("Antigravity context connection lost")
                        update = event.get("params", {}).get("update", {})
                        if update.get("sessionUpdate") == "available_commands_update":
                            commands_seen = True
                            supported = any(
                                c.get("name") == "compact"
                                for c in update.get("availableCommands", [])
                            )
                        usage_seen |= update.get("sessionUpdate") == "usage_update"
                        await self.router.context.acp_event(session, thread, event)
            except TimeoutError:
                pass
            if kind == "refresh":
                return
            if not supported:
                raise AgentError("native_compact_unsupported", "Antigravity 未声明 /compact。", 409)
            await self.router.context.compaction(
                session, "antigravity", thread, "running", manual=True
            )
            prompt = asyncio.create_task(
                rpc.request(
                    "session/prompt",
                    {"sessionId": thread, "prompt": [{"type": "text", "text": "/compact"}]},
                    timeout=None,
                )
            )
            try:
                while not prompt.done() or not rpc.events.empty():
                    receive = asyncio.create_task(rpc.events.get())
                    try:
                        await asyncio.wait([receive, prompt], return_when=asyncio.FIRST_COMPLETED)
                        if not receive.done():
                            continue
                        event = receive.result()
                    finally:
                        if not receive.done():
                            receive.cancel()
                            with suppress(asyncio.CancelledError):
                                await receive
                    if event.get("transport_error"):
                        raise ConnectionError("Antigravity compaction connection lost")
                    await self.router.context.acp_event(session, thread, event)
                    if "id" in event:
                        await rpc.send(
                            {
                                "id": event["id"],
                                "error": {"code": -32601, "message": "Context maintenance only"},
                            }
                        )
                        raise AgentError(
                            "context_unexpected_action", "压缩请求了额外操作，已停止。", 409
                        )
                if prompt.result().get("stopReason") != "end_turn":
                    raise AgentError("native_compact_failed", "Antigravity 压缩未完成。", 502)
                await self.router.context.compaction(session, "antigravity", thread, "completed")
            finally:
                if not prompt.done():
                    with suppress(Exception):
                        await rpc.send(
                            {"method": "session/cancel", "params": {"sessionId": thread}}
                        )
                prompt.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await prompt
