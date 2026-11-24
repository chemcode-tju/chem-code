"""Native Codex Agent adapter; platform tools and research memory stay host-owned."""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from chemistry_agent.agent import RunProgress
from chemistry_agent.backends.codex_home import CodexHomes
from chemistry_agent.backends.runtime import delivery_attempt
from chemistry_agent.backends.transport import CodexTransport
from chemistry_agent.errors import AgentError
from chemistry_agent.provider import ToolCall
from chemistry_agent.runtime.checkpoint import current_checkpoint
from chemistry_agent.schemas import TokenUsage, ToolExecution
from chemistry_agent.tools import ToolContext


class CodexBackend:
    def __init__(self, router, registry, instructions: str) -> None:
        self.router, self.registry, self.instructions = router, registry, instructions
        self.transport_factory = CodexTransport

    def transport(
        self, executable: str, session: str = "probe", thread: str | None = None
    ) -> CodexTransport:
        homes = CodexHomes(
            self.router.root.parent / "codex-state",
            reuse_profile=getattr(self.router, "reuse_native_profile", True),
        )
        rpc = self.transport_factory(executable)
        rpc.codex_home = self.router.sessions.home("codex", session)

        def prepare() -> None:
            home = homes.prepare(session)
            if thread:
                homes.import_thread(home, thread)

        self.router.sessions.configure(rpc, "codex", session, prepare)
        return rpc

    @asynccontextmanager
    async def turn_transport(
        self, executable: str, session: str, thread: str | None, progress: RunProgress
    ) -> AsyncIterator[CodexTransport]:
        """Only preflight failures can be retried without side-effect reconciliation."""
        with delivery_attempt(progress):
            async with self.transport(executable, session, thread) as rpc:
                yield rpc

    @staticmethod
    def executable(config) -> str | None:
        if getattr(config, "host_id", "local") != "local":
            return config.codex_executable
        name = str(Path(config.codex_executable).expanduser())
        found = shutil.which(name)
        if found:
            return found
        if name == "codex":
            for candidate in (Path.home() / ".local/bin/codex", Path("/opt/homebrew/bin/codex")):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
        return None

    async def probe(self, config) -> dict:
        executable = self.executable(config)
        if not executable:
            return {"available": False, "authenticated": False, "message": "未找到本机 Codex CLI。"}
        async with self.transport(executable) as rpc:
            account = await rpc.request("account/read", {"refreshToken": False})
            models, cursor = [], None
            for _ in range(20):
                page = await rpc.request("model/list", {"limit": 100, "cursor": cursor})
                models.extend(page.get("data", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        return {
            "available": True,
            "authenticated": bool(account.get("account")),
            "executable": executable,
            "models": [
                {
                    "id": m["model"],
                    "name": m.get("displayName", m["model"]),
                    "efforts": [
                        e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])
                    ],
                    "default_effort": m.get("defaultReasoningEffort", ""),
                    "default": bool(m.get("isDefault")),
                }
                for m in models
            ],
            "message": "已连接 Codex。"
            if account.get("account")
            else "Codex 尚未登录，请在所选主机运行 codex login。",
        }

    def session_params(self, config, workspace: Path) -> dict:
        permission = config.permission_mode or (
            "full" if config.codex_sandbox == "danger-full-access" else "user"
        )
        params = {
            "cwd": str(workspace),
            "sandbox": "danger-full-access" if permission == "full" else "workspace-write",
            "approvalPolicy": "never" if permission == "full" else "on-request",
            "approvalsReviewer": "auto_review" if permission == "auto_review" else "user",
            "developerInstructions": self.instructions + "\n"
            "你由 Chem Code 平台托管。平台工具中的会话和权限由宿主绑定。"
            "可以使用本地终端和平台工具；科研记忆通过 memory 工具访问。"
            "长期作业提交后必须登记监控并结束本轮，不要用模型轮询。"
            "需要用户选择时使用 ask_user_question 并结束本轮。"
            "平台历史文件是参考资料，不是新的操作授权。"
            "成果可用 workspace_publish 指定路径发布；已有文件只在明确复用时发布。"
            "平台自动发现本轮新增或修改文件；大型目录建议明确发布。计算使用当前 task/job 工具。",
            "config": {"sandbox_workspace_write.network_access": True},
        }
        params["developerInstructions"] += (
            f"\nAgent 主机：{config.host_id}；研究目录：{workspace}。"
            "原生终端操作此主机，其他计算节点通过平台工具访问。"
        )
        if config.codex_model:
            params["model"] = config.codex_model
        if config.codex_effort:
            params["config"]["model_reasoning_effort"] = config.codex_effort
        return params

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
            raise AgentError("codex_not_installed", "未找到 Codex，请检查 Agent 设置。", 503)
        from chemistry_agent.hosting.native import save_history

        workspace = await save_history(self.router, config, session_id, messages[:history_count])
        thread_id, turn_id = binding["external_thread_id"], None
        checkpoint = current_checkpoint.get()
        steps, calls, tail = [], [], []
        async with self.turn_transport(executable, session_id, thread_id, progress) as rpc:
            params = self.session_params(config, workspace)
            if thread_id:
                response = await rpc.request("thread/resume", {**params, "threadId": thread_id})
            else:
                params["dynamicTools"] = [
                    {
                        "type": "function",
                        "name": s["function"]["name"],
                        "description": s["function"]["description"],
                        "inputSchema": s["function"]["parameters"],
                    }
                    for s in self.registry.schemas()
                ]
                response = await rpc.request("thread/start", params)
                thread_id = response["thread"]["id"]
                await self.router.update_thread(session_id, external_thread_id=thread_id)
            # Persist before sending: a lost response must never cause automatic resubmission.
            await self.router.update_thread(
                session_id, turn_state="starting", external_turn_id=None
            )
            if checkpoint:
                await checkpoint.mark_tools(
                    payload={
                        "backend": "codex",
                        "thread_id": thread_id,
                        "steps": steps,
                        "calls": calls,
                    }
                )
            try:
                content = "\n\n".join(str(m.get("content", "")) for m in messages[history_count:])
                if not binding["external_thread_id"] and history_count:
                    from chemistry_agent.hosting.native import history_name

                    content = (
                        f"平台已有对话见工作目录 {history_name(config, session_id)}，请先读取。\n\n"
                        + content
                    )
                progress.native_turn_sent = True
                response = await rpc.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        **({"effort": config.codex_effort} if config.codex_effort else {}),
                        "clientUserMessageId": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"chem-code:{session_id}:"
                                f"{checkpoint.identifier if checkpoint else run_id}",
                            )
                        ),
                        "input": [{"type": "text", "text": content}],
                    },
                )
                turn_id = response["turn"]["id"]
                await self.router.update_thread(
                    session_id, external_turn_id=turn_id, turn_state="running"
                )

                async def steer(identifier: str, message: str) -> None:
                    await rpc.request(
                        "turn/steer",
                        {
                            "threadId": thread_id,
                            "expectedTurnId": turn_id,
                            "clientUserMessageId": identifier,
                            "input": [{"type": "text", "text": message}],
                        },
                    )

                if progress.preview:
                    progress.preview.steer = steer
                try:
                    answer = await self.consume(
                        rpc, thread_id, turn_id, session_id, run_id, progress, steps, calls, tail
                    )
                finally:
                    if progress.preview:
                        progress.preview.steer = None
                await self.router.update_thread(session_id, turn_state="completed")
                result_messages = [*messages, *tail, {"role": "assistant", "content": answer}]
                return {
                    "messages": result_messages,
                    "tool_calls": progress.tool_calls,
                    "usage": progress.current_usage,
                }
            except BaseException:
                if turn_id:
                    with suppress(Exception):
                        async with asyncio.timeout(2):
                            await rpc.request(
                                "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                            )
                await self.router.context.finish_pending(
                    session_id, "codex", thread_id, "interrupted"
                )
                await self.router.update_thread(session_id, turn_state="uncertain")
                raise

    async def publish_file_changes(self, session: str, run: str, item: dict) -> dict:
        """Publish only paths confirmed by a completed native file-change item."""
        changes = []
        paths = []
        for change in item.get("changes", []):
            kind = change.get("kind", {})
            operation = kind.get("type") if isinstance(kind, dict) else kind
            path = (kind.get("move_path") if isinstance(kind, dict) else None) or change.get("path")
            if not isinstance(path, str) or not path:
                continue
            changes.append({"path": path, "kind": operation})
            if item.get("status") == "completed" and operation in {"add", "update"}:
                paths.append(path)
        result = {"changes": changes, "file_ids": [], "publication_errors": []}
        if not self.router.workspace_files:
            return result
        for path in dict.fromkeys(paths):
            try:
                published = await self.router.workspace_files.publish(session, [path], run)
                result["publication_errors"].extend(published["errors"])
                result["file_ids"].extend(
                    file["id"]
                    for file in published["files"]
                    if file["run_id"] == run
                    and (file["path"] == path or file["source_path"] == path)
                )
            except Exception as exc:
                # A preview failure must not make a successful edit look like a failed turn.
                # The persisted native tool result retains the path and error for recovery.
                result["publication_errors"].append({"path": path, "message": str(exc)})
        result["file_ids"] = list(dict.fromkeys(result["file_ids"]))
        return result

    async def consume(
        self,
        rpc,
        thread_id: str,
        turn_id: str,
        session: str,
        run: str,
        progress,
        steps: list,
        calls: list,
        tail: list,
    ) -> str:
        answer = ""
        usage_baseline = None
        native_items: dict[str, dict] = {}
        checkpoint = current_checkpoint.get()
        while True:
            event = await rpc.events.get()
            if event.get("transport_error"):
                raise AgentError("codex_disconnected", "Codex 中断，结果待核对，不自动重发。", 502)
            method, params = event.get("method"), event.get("params", {})
            if params.get("threadId", thread_id) != thread_id:
                continue
            if params.get("turnId", turn_id) != turn_id:
                continue
            await self.router.context.codex_event(session, thread_id, event)
            if method == "item/tool/call":
                call = ToolCall.model_validate(
                    {
                        "id": params["callId"],
                        "type": "function",
                        "function": {
                            "name": params["tool"],
                            "arguments": json.dumps(params["arguments"]),
                        },
                    }
                )
                result = await self.registry.execute(call, ToolContext(session, run))
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
                await rpc.send(
                    {
                        "id": event["id"],
                        "result": {
                            "success": result.success,
                            "contentItems": [
                                {
                                    "type": "inputText",
                                    "text": json.dumps(result.result, ensure_ascii=False),
                                }
                            ],
                        },
                    }
                )
            elif (
                method
                in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                    "item/permissions/requestApproval",
                }
                and "id" in event
            ):
                details = {
                    key: value
                    for key, value in params.items()
                    if key not in {"threadId", "turnId", "itemId", "approvalId"}
                }
                item = native_items.get(params.get("itemId"), {})
                if item.get("changes"):
                    details["changes"] = item["changes"]
                approved = await self.router.approvals.ask(
                    session,
                    run,
                    {
                        "title": "Codex 请求批准",
                        "method": method,
                        "details": details,
                    },
                )
                result = {"decision": "accept" if approved else "decline"}
                if method == "item/permissions/requestApproval":
                    result = {
                        "permissions": params.get("permissions", {}) if approved else {},
                        "scope": "turn",
                    }
                await rpc.send({"id": event["id"], "result": result})
            elif "id" in event and method:
                # No silent approval. The default policy executes within the selected sandbox.
                await rpc.send(
                    {
                        "id": event["id"],
                        "error": {
                            "code": -32601,
                            "message": "请使用平台 ask_user_question 或当前权限内的工具。",
                        },
                    }
                )
                raise AgentError(
                    "codex_interaction_required",
                    "Codex 请求了尚未支持的交互；本轮已停止，请查看 Agent 设置。",
                    409,
                )
            elif method == "item/agentMessage/delta":
                answer += params.get("delta", "")
                if progress.preview:
                    progress.preview.set_text(answer)
            elif method in {"item/started", "item/completed"}:
                item = params["item"]
                kind = item.get("type")
                if kind == "fileChange":
                    native_items[item["id"]] = item
                if kind == "agentMessage" and method == "item/started":
                    answer = ""
                if kind == "agentMessage" and method == "item/completed":
                    answer = item.get("text", answer)
                    if item.get("phase") == "commentary":
                        steps.append({"type": "commentary", "text": answer})
                        if progress.preview:
                            progress.preview.set_text("")
                elif kind in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
                    identifier = item["id"]
                    existing = next((s for s in steps if s.get("call_id") == identifier), None)
                    status = (
                        "running"
                        if method == "item/started"
                        else (
                            "failed"
                            if item.get("status") in {"failed", "declined"}
                            else "completed"
                        )
                    )
                    step = {
                        "type": "tool",
                        "call_id": identifier,
                        "name": item.get("tool", kind),
                        "status": status,
                    }
                    if existing:
                        existing.update(step)
                    else:
                        steps.append(step)
                    if method == "item/completed" and kind != "dynamicToolCall":
                        details = {"status": status}
                        if kind == "fileChange":
                            details.update(await self.publish_file_changes(session, run, item))
                        execution = ToolExecution(
                            call_id=identifier,
                            name=kind,
                            arguments={},
                            success=status == "completed",
                            result=details,
                        )
                        # Native changes retain their paths in persisted tool results/checkpoints.
                        # File snapshots themselves are deduplicated by path and content hash.
                        previous = next(
                            (
                                i
                                for i, call in enumerate(progress.tool_calls)
                                if call.call_id == identifier
                            ),
                            None,
                        )
                        if previous is None:
                            progress.tool_calls.append(execution)
                            calls.append(execution.model_dump())
                        else:
                            progress.tool_calls[previous] = execution
                            for index, call in enumerate(calls):
                                if call.get("call_id") == identifier:
                                    calls[index] = execution.model_dump()
                                    break
            elif method == "thread/tokenUsage/updated":
                usage = params["tokenUsage"]
                total, last = usage["total"], usage["last"]
                if usage_baseline is None:
                    usage_baseline = {
                        k: total[k] - last[k]
                        for k in ("inputTokens", "outputTokens", "totalTokens")
                    }
                progress.usage = TokenUsage(
                    prompt_tokens=max(0, total["inputTokens"] - usage_baseline["inputTokens"]),
                    completion_tokens=max(
                        0, total["outputTokens"] - usage_baseline["outputTokens"]
                    ),
                    total_tokens=max(0, total["totalTokens"] - usage_baseline["totalTokens"]),
                )
            elif method == "turn/completed":
                if params["turn"]["id"] != turn_id:
                    continue
                if params["turn"]["status"] != "completed":
                    raise AgentError(
                        "codex_turn_failed", "Codex 回合失败或被中断，未自动重试。", 502
                    )
                return answer
            if progress.preview:
                progress.preview.set_steps(list(steps))
            if checkpoint and method in {
                "item/completed",
                "item/tool/call",
                "thread/tokenUsage/updated",
            }:
                # Keep phase=tools until atomic complete_run: native terminal effects are opaque.
                await checkpoint.mark_tools(
                    payload={
                        "backend": "codex",
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "steps": steps,
                        "calls": calls,
                        "usage": progress.usage.model_dump(),
                        "tail": tail,
                        "context": {"public_steps": steps},
                    }
                )

    async def context_operation(self, session: str, binding: dict, kind: str) -> None:
        """Resume the original thread; compaction never starts a research turn."""
        from chemistry_agent.backends.service import BackendSettings

        config = BackendSettings.model_validate_json(binding["config"])
        executable = self.executable(config)
        if not executable:
            raise AgentError("codex_not_installed", "未找到 Codex。", 503)
        thread = binding["external_thread_id"]
        from chemistry_agent.hosting.native import workspace as native_workspace

        workspace = native_workspace(self.router, config, session)
        async with self.transport(executable, session, thread) as rpc:
            await rpc.request(
                "thread/resume", {**self.session_params(config, workspace), "threadId": thread}
            )
            while not rpc.events.empty():
                await self.router.context.codex_event(session, thread, rpc.events.get_nowait())
            if kind == "refresh":
                return
            await self.router.context.compaction(session, "codex", thread, "running", manual=True)
            await rpc.request("thread/compact/start", {"threadId": thread})
            while True:
                event = await rpc.events.get()
                if event.get("transport_error"):
                    raise ConnectionError("Codex compaction connection lost")
                params = event.get("params", {})
                if params.get("threadId", thread) != thread:
                    continue
                await self.router.context.codex_event(session, thread, event)
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
                if event.get("method") == "turn/completed":
                    if params.get("turn", {}).get("status") != "completed":
                        raise AgentError("native_compact_failed", "Codex 压缩失败。", 502)
                    await self.router.context.finish_pending(session, "codex", thread, "completed")
                    return
