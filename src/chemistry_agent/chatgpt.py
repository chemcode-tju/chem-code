"""ChatGPT subscription access through the official Codex CLI, not copied tokens."""

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chemistry_agent.chatgpt_plan import ChatGPTPlan
from chemistry_agent.chatgpt_responses import PlanResponses
from chemistry_agent.errors import AgentError
from chemistry_agent.model_settings import ModelSettings, ModelSettingsStore
from chemistry_agent.provider import AssistantMessage, Completion
from chemistry_agent.schemas import TokenUsage

LOGIN_TIMEOUT = 900
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()


@dataclass
class LoginAttempt:
    state: str = "starting"
    url: str | None = None
    code: str | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict:
        data = {"state": self.state, "url": self.url, "code": self.code}
        if self.error:
            data["error"] = self.error
        return data


class ChatGPTClient:
    def __init__(self, store: ModelSettingsStore):
        self.store = store
        self.plan = ChatGPTPlan(store)
        self.responses = PlanResponses(self.plan)
        self.attempts: dict[str | None, LoginAttempt] = {}
        self.locks: dict[str | None, asyncio.Lock] = {}

    def lock(self, user_id: str | None) -> asyncio.Lock:
        return self.locks.setdefault(user_id, asyncio.Lock())

    def environment(self, user_id: str | None) -> dict[str, str]:
        directory = self.store.directory(user_id) / "codex"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        # Do not pass server API keys, inherited model gateways, or desktop session identity.
        names = {
            "HOME",
            "PATH",
            "TMPDIR",
            "LANG",
            "SYSTEMROOT",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        }
        environment = {name: value for name, value in os.environ.items() if name in names}
        environment["CODEX_HOME"] = str(directory)
        return environment

    def executable(self) -> str:
        executable = shutil.which(self.store.settings.codex_binary)
        if not executable:
            raise AgentError(
                "codex_not_installed", "未找到 Codex CLI，请在服务所在电脑安装并重启服务。", 503
            )
        return executable

    async def process(
        self, user_id: str | None, *arguments: str, **options: Any
    ) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                self.executable(),
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                'cli_auth_credentials_store="file"',
                *arguments,
                env=self.environment(user_id),
                **options,
            )
        except OSError as exc:
            raise AgentError(
                "codex_start_failed", "无法启动 Codex CLI，请检查安装和执行权限。", 503
            ) from exc

    async def authenticated(self, user_id: str | None) -> bool:
        if self.plan.selected(user_id):
            return self.plan.authenticated(user_id)
        if not shutil.which(self.store.settings.codex_binary):
            return False
        if not (self.store.directory(user_id) / "codex" / "auth.json").is_file():
            return False
        process = await self.process(
            user_id,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
            return process.returncode == 0 and "chatgpt" in output.decode(errors="replace").lower()
        except TimeoutError:
            return False
        finally:
            await stop_process(process)

    async def status(self, user_id: str | None) -> dict:
        attempt = self.attempts.get(user_id)
        logged_in = await self.authenticated(user_id)
        return {
            "available": True,
            "legacy_available": bool(shutil.which(self.store.settings.codex_binary)),
            "authentication_mode": (
                "chemistry-agent"
                if self.plan.selected(user_id)
                else "codex"
                if logged_in
                else "none"
            ),
            **self.plan.public(user_id),
            "authenticated": logged_in,
            "login": attempt.public() if attempt else {"state": "idle", "url": None, "code": None},
        }

    async def start_login(
        self,
        user_id: str | None,
        method: str,
        *,
        account_id: str | None = None,
        new_account: bool = False,
    ) -> dict:
        async with self.lock(user_id):
            if method == "device":
                self.executable()
            await self.cancel_login(user_id)
            attempt = LoginAttempt()
            self.attempts[user_id] = attempt
            attempt.task = asyncio.create_task(
                self._plan_login(user_id, attempt, account_id=account_id, new_account=new_account)
                if method == "browser"
                else self._login(user_id, method, attempt)
            )
            return attempt.public()

    async def _plan_login(self, user_id: str | None, attempt: LoginAttempt, **options: Any) -> None:
        try:
            await self.plan.login(user_id, attempt, **options)
        except TimeoutError:
            attempt.state = "expired"
        except asyncio.CancelledError:
            attempt.state = "cancelled"
            raise
        except AgentError as exc:
            attempt.state = "failed"
            attempt.error = exc.message
        except (OSError, ValueError, KeyError, TypeError):
            attempt.state = "failed"
            attempt.error = "授权处理失败，请重新登录。"
        finally:
            attempt.url = attempt.code = None

    async def select_account(self, user_id: str | None, account_id: str) -> None:
        async with self.lock(user_id):
            await self.cancel_login(user_id)
            await self.plan.select_account(user_id, account_id)

    async def _login(self, user_id: str | None, method: str, attempt: LoginAttempt) -> None:
        process = None
        try:
            arguments = ["login"] + (["--device-auth"] if method == "device" else [])
            process = await self.process(
                user_id,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(LOGIN_TIMEOUT):
                while line := await process.stdout.readline():
                    text = ANSI.sub("", line.decode(errors="replace"))
                    # Only expose the official authorization URL and one-time user code.
                    match = re.search(r"https://auth\.openai\.com/[A-Za-z0-9_/?=&%+.#~-]+", text)
                    if match:
                        attempt.url = match.group(0)
                        attempt.state = "waiting"
                    match = re.search(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b", text)
                    if match:
                        attempt.code = match.group(0)
                await process.wait()
                attempt.state = "complete" if process.returncode == 0 else "failed"
                if attempt.state == "complete":
                    async with self.plan.locked(user_id):
                        data = self.plan.load(user_id)
                        data["selected"] = False
                        self.plan.save(user_id, data)
        except TimeoutError:
            attempt.state = "expired"
        except asyncio.CancelledError:
            attempt.state = "cancelled"
            raise
        except (OSError, AgentError, ValueError):
            attempt.state = "failed"
        finally:
            if process is not None:
                await stop_process(process)
            if attempt.state != "waiting":
                attempt.url = attempt.code = None

    async def cancel_login(self, user_id: str | None) -> None:
        attempt = self.attempts.get(user_id)
        if attempt and attempt.task and not attempt.task.done():
            attempt.task.cancel()
            await asyncio.gather(attempt.task, return_exceptions=True)

    async def logout(self, user_id: str | None) -> dict:
        async with self.lock(user_id):
            await self.cancel_login(user_id)
            if self.plan.selected(user_id):
                self.attempts.pop(user_id, None)
                return await self.plan.logout(user_id)
            process = await self.process(
                user_id,
                "logout",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), 10)
                if process.returncode:
                    raise AgentError("chatgpt_logout_failed", "退出失败，请稍后重试。", 503)
            finally:
                await stop_process(process)
            self.attempts.pop(user_id, None)
            return {"remote_revoked": True}

    async def list_models(self, user_id: str | None) -> list[dict]:
        async with self.lock(user_id):
            if self.plan.selected(user_id):
                return await self.responses.list_models(user_id)
            if not await self.authenticated(user_id):
                raise AgentError("chatgpt_login_required", "请先在设置中登录 ChatGPT。", 503)
            process = await self.process(
                user_id,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def rpc(identifier: int, method: str, params: dict) -> dict:
                process.stdin.write(
                    (
                        json.dumps({"id": identifier, "method": method, "params": params}) + "\n"
                    ).encode()
                )
                await process.stdin.drain()
                while line := await process.stdout.readline():
                    result = json.loads(line)
                    if result.get("id") == identifier:
                        if "error" in result:
                            raise ValueError("Catalog RPC failed")
                        return result["result"]
                raise ValueError("Catalog RPC closed")

            try:
                async with asyncio.timeout(30):
                    await rpc(
                        0,
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "chemistry_agent",
                                "title": "Chemistry Agent",
                                "version": "0.1.0",
                            }
                        },
                    )
                    process.stdin.write(b'{"method":"initialized"}\n')
                    await process.stdin.drain()
                    models, cursor = [], None
                    for page in range(20):
                        result = await rpc(
                            page + 1,
                            "model/list",
                            {"limit": 100, "includeHidden": False, "cursor": cursor},
                        )
                        models.extend(
                            {"id": item["model"], "name": item.get("displayName") or item["model"]}
                            for item in result["data"]
                            if not item.get("hidden")
                        )
                        cursor = result.get("nextCursor")
                        if not cursor:
                            return models
                    raise ValueError("Catalog pagination exceeded")
            except (ValueError, KeyError, TypeError, TimeoutError, OSError) as exc:
                raise AgentError(
                    "model_catalog_failed", "无法读取模型目录，请刷新或手动填写模型 ID。", 502
                ) from exc
            finally:
                await stop_process(process)

    async def close(self) -> None:
        for user_id in list(self.attempts):
            await self.cancel_login(user_id)

    async def complete(
        self,
        user_id: str | None,
        configuration: ModelSettings,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        allow_tools: bool,
        max_tokens: int | None = None,
        thinking: bool | None = None,
    ) -> Completion:
        """Request a tool-call plan; only Chemistry Agent executes registered tools."""
        async with self.lock(user_id):
            if self.plan.selected(user_id):
                return await self.responses.complete(
                    user_id,
                    configuration.model,
                    messages,
                    tools,
                    allow_tools=allow_tools,
                    max_tokens=max_tokens,
                    thinking=thinking,
                )
            if not await self.authenticated(user_id):
                raise AgentError("chatgpt_login_required", "请在模型设置中登录 ChatGPT。", 503)
            with tempfile.TemporaryDirectory(prefix="chemistry-response-") as temporary:
                directory = Path(temporary)
                schema = directory / "schema.json"
                schema.write_text(json.dumps(response_schema()))
                output = directory / "response.json"
                arguments = [
                    "exec",
                    "--ignore-user-config",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--color",
                    "never",
                    "--json",
                    "--cd",
                    str(directory),
                    "--output-schema",
                    str(schema),
                    "--output-last-message",
                    str(output),
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    "project_doc_max_bytes=0",
                ]
                for feature in (
                    "shell_tool",
                    "unified_exec",
                    "apps",
                    "plugins",
                    "multi_agent",
                    "browser_use",
                    "computer_use",
                    "image_generation",
                    "view_image",
                    "memories",
                    "hooks",
                    "code_mode",
                    "code_mode_host",
                    "skill_search",
                ):
                    arguments.extend(["--disable", feature])
                if configuration.model:
                    arguments.extend(["--model", configuration.model])
                arguments.append("-")
                prompt = (
                    "Act as the model backend for Chemistry Agent. Return ONLY the required JSON. "
                    "Follow the system messages in the supplied conversation. Do not execute tools "
                    "yourself. Request only listed functions using tool_calls with unique "
                    "IDs and JSON-encoded arguments; the application executes and returns results. "
                    "If no tool is needed, return content and an empty tool_calls array. "
                    "Never claim a computation succeeded without a supplied tool result. "
                    + (
                        f"Keep the response within approximately {max_tokens} tokens.\n"
                        if max_tokens is not None
                        else "Keep prose concise; include complete code and tool arguments. "
                        "Do not truncate executable plans to shorten the response.\n"
                    )
                    + json.dumps(
                        {"messages": messages, "tools": tools if allow_tools else []},
                        ensure_ascii=False,
                    )
                )
                process = await self.process(
                    user_id,
                    *arguments,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    events, _ = await asyncio.wait_for(
                        process.communicate(prompt.encode()),
                        self.store.settings.codex_timeout_seconds,
                    )
                    if process.returncode:
                        raise AgentError(
                            "chatgpt_response_failed",
                            "ChatGPT 请求失败，请检查登录、模型权限及额度。",
                            502,
                        )
                    message = AssistantMessage.model_validate_json(output.read_text())
                    names = {tool["function"]["name"] for tool in tools} if allow_tools else set()
                    ids = [call.id for call in message.tool_calls]
                    if (
                        len(ids) != len(set(ids))
                        or any(call.function.name not in names for call in message.tool_calls)
                        or (not message.tool_calls and not (message.content or "").strip())
                    ):
                        raise ValueError("Invalid model response")
                    return Completion(message=message, usage=event_usage(events))
                except TimeoutError as exc:
                    raise AgentError(
                        "provider_timeout", "ChatGPT 响应超时，请稍后重试。", 504
                    ) from exc
                except (ValueError, OSError) as exc:
                    raise AgentError(
                        "provider_invalid_response", "ChatGPT 返回了无法解析的响应。", 502
                    ) from exc
                finally:
                    await stop_process(process)


def response_schema() -> dict:
    """Codex structured output requires every object property to be required."""
    schema = AssistantMessage.model_json_schema()

    def strict(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in list(node.values()):
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    # Never request private reasoning in structured output.
    schema["properties"].pop("reasoning_content", None)
    strict(schema)
    return schema


def event_usage(events: bytes) -> TokenUsage:
    usage = TokenUsage()
    for line in events.splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "turn.completed":
                data = event.get("usage", {})
                prompt = data.get("input_tokens", 0)
                completion = data.get("output_tokens", 0)
                usage = usage.plus(
                    TokenUsage(
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        total_tokens=prompt + completion,
                    )
                )
        except (ValueError, TypeError, AttributeError):
            continue
    return usage
