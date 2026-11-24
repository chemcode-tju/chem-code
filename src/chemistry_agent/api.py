import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from chemistry_agent import __version__
from chemistry_agent.agent import (
    AUTOMATION_PROMPT,
    MEMORY_PROMPT,
    SYSTEM_PROMPT,
    ChemistryAgent,
    RunProgress,
)
from chemistry_agent.attachments import attachment_context
from chemistry_agent.attachments import router as attachments_router
from chemistry_agent.auth import COOKIE_NAME, AuthenticationMiddleware, AuthManager
from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.chat_routes import router as calculation_chat_router
from chemistry_agent.automation.job_history import JobHistory
from chemistry_agent.automation.monitor_schedules import router as monitor_wake_router
from chemistry_agent.automation.monitor_schedules import scheduler
from chemistry_agent.automation.routes import local_service
from chemistry_agent.automation.routes import router as automation_router
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.automation.settings_tools import authorizes_configuration, configuration_grant
from chemistry_agent.automation.ssh_routes import router as ssh_router
from chemistry_agent.backends.groups import run_group, turn_members
from chemistry_agent.backends.handoff import HandoffInput, handoff
from chemistry_agent.backends.routes import router as backend_router
from chemistry_agent.backends.service import BackendRouter
from chemistry_agent.chat_queue import ChatQueue
from chemistry_agent.config import Settings
from chemistry_agent.context import ContextManager
from chemistry_agent.environments.routes import router as environments_router
from chemistry_agent.errors import AgentError
from chemistry_agent.file_browser import BrowserConnections
from chemistry_agent.file_browser import router as file_browser_router
from chemistry_agent.knowledge.mcp import router as knowledge_mcp_router
from chemistry_agent.knowledge.routes import router as knowledge_router
from chemistry_agent.knowledge.service import KNOWLEDGE_PROMPT, Knowledge
from chemistry_agent.knowledge.tools import knowledge_tools
from chemistry_agent.memory.routes import router as memory_router
from chemistry_agent.memory.tools import memory_tools
from chemistry_agent.model_routes import router as model_router
from chemistry_agent.model_runtime import ProviderRouter
from chemistry_agent.platform.lease import RuntimeLease
from chemistry_agent.platform.routes import router as platform_router
from chemistry_agent.projects import router as projects_router
from chemistry_agent.provider import ToolCall
from chemistry_agent.runtime.search import web_search_tool
from chemistry_agent.runtime.shell import authorized_host, remote_shell_tool, shell_grant
from chemistry_agent.schemas import (
    ChatRequest,
    ChatResponse,
    QueuedChatRequest,
    RunDetail,
    SessionCreate,
    SessionDetail,
    SessionSummary,
    StrictModel,
    ToolExecution,
)
from chemistry_agent.session_titles import router as titles_router
from chemistry_agent.store import Store
from chemistry_agent.structures import router as structures_router
from chemistry_agent.tokens import TokenCounter
from chemistry_agent.tools import Tool, ToolRegistry
from chemistry_agent.user_questions import QUESTION_TOOL, Ask, Reply
from chemistry_agent.workspace_files import WorkspaceFiles, workspace_tools
from chemistry_agent.workspace_files import router as workspace_router

logger = logging.getLogger(__name__)
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"


class ToolRequest(StrictModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    prepare_local: Callable[[BackendRouter, ProviderRouter], Awaitable[None]] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    automation = AutomationService(settings)
    calculation_chat = CalculationChat(automation)
    calculation_enabled = settings.automation_enabled and not settings.auth_enabled
    extras = calculation_chat.tools() if calculation_enabled else []
    extras.append(QUESTION_TOOL)
    if not settings.auth_enabled:
        extras.append(Tool(
            "group_handoff", "Transfer this assignment to another team member, then end your turn.",
            HandoffInput, handoff,
        ))
    extras.extend(workspace_tools(lambda: app.state.workspace_files))
    if settings.research_memory_enabled and not settings.auth_enabled:
        extras.extend(
            knowledge_tools(
                lambda: app.state.knowledge,
                lambda session: app.state.agent_backends.workspace_directory(session),
            )
        )
    if settings.research_memory_enabled:
        extras.extend(memory_tools(lambda: app.state.store))
    if settings.web_search_enabled:
        extras.append(web_search_tool(settings.search_endpoint))
    ssh_hosts = settings.allowed_ssh_hosts
    ssh_root = settings.platform_path if calculation_enabled else None
    if ssh_hosts or calculation_enabled:
        extras.append(remote_shell_tool(ssh_hosts, ssh_root))

    def available_hosts() -> tuple[str, ...]:
        from chemistry_agent.environments.registry import Environments

        return tuple(Environments(ssh_root).available_hosts(ssh_hosts)) if ssh_root else ssh_hosts

    registry = ToolRegistry(extras)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with RuntimeLease(settings) if calculation_enabled else nullcontext():
            store = await Store.open(settings.database_path)
            store.research_memory_enabled = settings.research_memory_enabled
            try:
                provider = ProviderRouter(settings, transport=transport)
                try:
                    app.state.title_locks = WeakValueDictionary()
                    app.state.provider = provider
                    app.state.store = store
                    app.state.knowledge = Knowledge(
                        store,
                        settings.platform_path,
                        settings.database_path.parent / "agent-workspaces",
                        allowed_hosts=settings.allowed_ssh_hosts,
                    )
                    await app.state.knowledge.sync.initialize()
                    app.state.workspace_files = WorkspaceFiles(
                        store, settings.database_path.parent / "agent-workspaces"
                    )
                    await app.state.workspace_files.initialize()
                    calculation_chat.attachment_store = store
                    app.state.auth = AuthManager(settings, store)
                    counter = await asyncio.to_thread(TokenCounter, settings.tokenizer_path)
                    context_manager = ContextManager(
                        settings,
                        provider,
                        store,
                        counter,
                        SYSTEM_PROMPT
                        + (AUTOMATION_PROMPT if calculation_enabled else "")
                        + (
                            KNOWLEDGE_PROMPT
                            if settings.research_memory_enabled and not settings.auth_enabled
                            else ""
                        )
                        + (MEMORY_PROMPT if settings.research_memory_enabled else ""),
                        registry.schemas(),
                    )
                    app.state.agent = ChemistryAgent(
                        provider,
                        registry,
                        settings.max_tool_rounds,
                        context_manager,
                    )
                    app.state.agent_backends = BackendRouter(
                        store,
                        app.state.agent,
                        registry,
                        settings.database_path.parent / "agent-workspaces",
                        SYSTEM_PROMPT
                        + (AUTOMATION_PROMPT if calculation_enabled else "")
                        + (
                            KNOWLEDGE_PROMPT
                            if settings.research_memory_enabled and not settings.auth_enabled
                            else ""
                        )
                        + (MEMORY_PROMPT if settings.research_memory_enabled else ""),
                    )
                    from chemistry_agent.hosting.registry import Hosting

                    app.state.agent_backends.hosting = Hosting(
                        settings.platform_path, settings.allowed_ssh_hosts
                    )
                    await app.state.agent_backends.initialize()
                    if prepare_local and not settings.auth_enabled:
                        await prepare_local(app.state.agent_backends, provider)
                    app.state.agent_backends.workspace_files = app.state.workspace_files
                    app.state.workspace_files.sync_remote = app.state.agent_backends.sync_workspace
                    app.state.context_manager = context_manager
                    app.state.chat_queue = ChatQueue(store, chat, provider)
                    # Historical event records remain available; no legacy daemon is started.
                    app.state.task_monitor = None
                    calculation_chat.monitor = None
                    if calculation_enabled:
                        calculation_chat.plans.start(
                            store,
                            app.state.agent_backends if settings.platform_agent_enabled else None,
                            provider if settings.platform_agent_enabled else None,
                        )
                    await app.state.chat_queue.recover(app)
                    app.state.browser_connections = BrowserConnections(settings.platform_path)
                    app.state.job_history = JobHistory(calculation_chat.plans.slurm)
                    if calculation_enabled:
                        app.state.job_history.start()
                    if settings.research_memory_enabled:
                        app.state.knowledge.sync.start()
                    try:
                        async with scheduler(
                            app, calculation_enabled and settings.platform_agent_enabled
                        ):
                            yield
                    finally:
                        await app.state.job_history.close()
                        await app.state.browser_connections.close()
                        await app.state.chat_queue.close()
                        await app.state.agent_backends.context.close()
                        await app.state.knowledge.sync.close()
                        await calculation_chat.plans.close()
                finally:
                    await provider.close()
            finally:
                await store.close()

    app = FastAPI(
        title="Chem Code",
        version=__version__,
        lifespan=lifespan,
        description="化学研究助手：ChatGPT 账号登录或自选模型 API + LangGraph + RDKit。",
    )
    app.mount("/assets", StaticFiles(directory=WEB_DIRECTORY), name="assets")
    app.include_router(model_router)
    app.include_router(backend_router)
    app.include_router(projects_router)
    app.include_router(memory_router)
    app.include_router(titles_router)
    app.include_router(attachments_router)
    app.include_router(workspace_router)
    app.include_router(file_browser_router)
    from chemistry_agent.guidance import router as guidance_router
    from chemistry_agent.hosting.routes import router as hosting_router
    from chemistry_agent.hosting.terminal import router as terminal_router
    from chemistry_agent.workspace_chat import router as workspace_chat_router

    app.include_router(guidance_router)
    app.include_router(workspace_chat_router)
    app.include_router(terminal_router)
    app.include_router(hosting_router)
    app.include_router(structures_router)

    app.state.settings = settings
    app.state.automation = automation
    app.state.calculation_chat = calculation_chat
    app.state.platform = calculation_chat.plans.platform
    from chemistry_agent.automation.queue_browser import router as queue_router

    app.include_router(monitor_wake_router)
    app.include_router(queue_router)
    app.include_router(automation_router)
    app.include_router(ssh_router)
    app.include_router(calculation_chat_router)
    app.include_router(platform_router)
    app.include_router(knowledge_router)
    app.include_router(knowledge_mcp_router)
    app.include_router(environments_router)
    app.add_middleware(AuthenticationMiddleware, settings=settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Pydantic's default error includes rejected input, potentially a plaintext key.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": error["loc"],
                        "type": error["type"],
                        "msg": "输入格式有误，请检查字段。",
                    }
                    for error in exc.errors()
                ]
            },
        )

    @app.get("/auth/status", tags=["accounts"])
    async def auth_status(request: Request):
        return {"enabled": settings.auth_enabled, "user": request.state.user}

    @app.post("/auth/login", tags=["accounts"])
    async def login(body: LoginRequest, request: Request, response: Response):
        if not settings.auth_enabled:
            raise AgentError("authentication_disabled", "当前为本地单用户模式。", 400)
        user, token = await request.app.state.auth.login(
            body.username,
            body.password.get_secret_value(),
            request.client.host if request.client else "unknown",
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.session_ttl_seconds,
            path="/",
        )
        return {"user": user}

    @app.post("/auth/logout", tags=["accounts"], status_code=204)
    async def logout(request: Request):
        await request.app.state.auth.logout(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(
            COOKIE_NAME, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/"
        )
        return response

    @app.exception_handler(AgentError)
    async def agent_error_handler(request: Request, exc: AgentError):
        detail = {"code": exc.code, "message": exc.message}
        for name in ("run_id", "session_id"):
            if value := getattr(request.state, name, None):
                detail[name] = value
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.get("/", include_in_schema=False)
    @app.get("/memory", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIRECTORY / "index.html")

    @app.get("/monitor", include_in_schema=False)
    @app.get("/monitor/tasks", include_in_schema=False)
    @app.get("/monitor/board", include_in_schema=False)
    async def job_monitor(request: Request):
        local_service(request)
        return FileResponse(WEB_DIRECTORY / "index.html")

    @app.get("/structures", include_in_schema=False)
    async def structure_viewer():
        return FileResponse(WEB_DIRECTORY / "structure" / "index.html")

    @app.get("/agents", include_in_schema=False)
    async def agent_settings_page(request: Request):
        parameters = urlencode(
            [
                (key, request.query_params[key])
                for key in ("session", "host")
                if key in request.query_params
            ]
        )
        return RedirectResponse(f"/settings{'?' + parameters if parameters else ''}#agent")

    @app.get("/health", tags=["service"])
    async def health(request: Request, session_id: UUID | None = None):
        provider = request.app.state.provider
        _, configuration = provider.configuration()
        try:
            local_service(request)
            local_shell_access = True
        except AgentError:
            local_shell_access = False
        backend = "builtin"
        identifier = str(session_id) if session_id else None
        if local_shell_access:
            if identifier:
                await request.app.state.store.require_session(identifier, request.state.user_id)
            binding = await request.app.state.agent_backends.binding(identifier)
            backend = binding["backend"]
        if settings.auth_enabled and not request.state.user:
            ready = False
        elif backend != "builtin":
            ready = await request.app.state.agent_backends.ready(identifier)
        else:
            ready = await provider.ready()
        return {
            "agent_backend": backend,
            "status": "ok",
            "version": __version__,
            "model_configured": ready,
            "model": ("本地 " + request.app.state.agent_backends.descriptors[backend][0])
            if backend != "builtin"
            else configuration.model or "ChatGPT",
            "provider": backend if backend != "builtin" else configuration.provider,
            "thinking": configuration.thinking,
            "context_window_tokens": settings.context_window_tokens,
            "context_input_budget_tokens": settings.context_input_budget,
            "context_recent_turns": settings.context_recent_turns,
            "automation_enabled": calculation_enabled,
            "queue_monitor_enabled": calculation_enabled and local_shell_access,
            "ssh_hosts": available_hosts() if local_shell_access else (),
        }

    @app.get("/tools", tags=["tools"])
    async def tools():
        return registry.schemas()

    @app.post("/tools/{name}/invoke", response_model=ToolExecution, tags=["tools"])
    async def invoke_tool(name: str, body: ToolRequest):
        if name not in registry.tools:
            raise AgentError("tool_not_found", "工具不存在。", 404)
        call = ToolCall.model_validate(
            {
                "id": str(uuid4()),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(body.arguments, ensure_ascii=False),
                },
            }
        )
        result = await registry.execute(call)
        if not result.success:
            return JSONResponse(status_code=422, content=result.model_dump())
        return result

    @app.post("/sessions", response_model=SessionSummary, status_code=201, tags=["sessions"])
    async def create_session(body: SessionCreate, request: Request):
        session = await request.app.state.store.create_session(body.title, request.state.user_id)
        await request.app.state.agent_backends.binding(session["id"], create=True)
        return session

    @app.get("/sessions", response_model=list[SessionSummary], tags=["sessions"])
    async def list_sessions(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return await request.app.state.store.list_sessions(limit, offset, request.state.user_id)

    @app.get("/sessions/{session_id}", response_model=SessionDetail, tags=["sessions"])
    async def get_session(session_id: UUID, request: Request):
        return await request.app.state.store.get_session(str(session_id), request.state.user_id)

    @app.get("/sessions/{session_id}/runs", response_model=list[RunDetail], tags=["runs"])
    async def list_runs(
        session_id: UUID,
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        compact: bool = False,
    ):
        return await request.app.state.store.list_runs(
            str(session_id), limit, offset, request.state.user_id, compact=compact
        )

    @app.get("/sessions/{session_id}/context", tags=["sessions"])
    async def context_status(session_id: UUID, request: Request):
        store = request.app.state.store
        async with store.lock:
            owned = await store._one(
                "SELECT id FROM sessions WHERE id=? AND owner_id IS ?",
                (str(session_id), request.state.user_id),
            )
        if owned is None:
            raise AgentError("session_not_found", "会话不存在。", 404)
        binding = await request.app.state.agent_backends.binding(str(session_id))
        if binding["backend"] != "builtin":
            return await request.app.state.agent_backends.context.snapshot(str(session_id), binding)
        session = await store.get_session(str(session_id), request.state.user_id)
        memory = await store.get_context_memory(str(session_id))
        manager = request.app.state.context_manager
        provider = request.app.state.provider
        _, configuration = provider.configuration()
        model = await store.get_session_model(
            str(session_id), request.state.user_id, configuration.provider, configuration.base_url
        )
        provider.select_model(model)
        budget = await manager.window() - settings.deepseek_max_tokens
        wire, compacted = manager._wire(session["messages"], [], memory)
        return {
            **memory.model_dump(),
            "original_messages": len(session["messages"]),
            "estimated_input_tokens": manager.counter.request_tokens(wire, manager.tools),
            "input_budget_tokens": budget,
            "trigger_tokens": int(budget * settings.context_trigger_ratio),
            "recent_turns": settings.context_recent_turns,
            "compacted_tool_results": compacted,
            "tokenizer": manager.counter.method,
        }

    @app.post("/sessions/{session_id}/context/refresh", status_code=202, tags=["sessions"])
    async def refresh_native_context(session_id: UUID, request: Request, operation_id: UUID):
        local_service(request)
        await request.app.state.store.get_session(str(session_id), request.state.user_id)
        return await request.app.state.agent_backends.context.start(
            str(session_id), request.state.user_id, "refresh", str(operation_id)
        )

    @app.post("/sessions/{session_id}/context/compact", tags=["sessions"])
    async def compact_context(session_id: UUID, request: Request, operation_id: UUID | None = None):
        await request.app.state.store.get_session(str(session_id), request.state.user_id)
        binding = await request.app.state.agent_backends.binding(str(session_id))
        if binding["backend"] != "builtin":
            local_service(request)
            return JSONResponse(
                status_code=202,
                content=await request.app.state.agent_backends.context.start(
                    str(session_id), request.state.user_id, "compact", str(operation_id or uuid4())
                ),
            )
        if calculation_enabled:
            local_service(request)
        provider = request.app.state.provider
        _, configuration = provider.configuration()
        model = await request.app.state.store.get_session_model(
            str(session_id), request.state.user_id, configuration.provider, configuration.base_url
        )
        provider.select_model(model)
        if not await provider.ready():
            raise AgentError("model_not_configured", "请先配置模型以生成历史摘要。", 503)
        store = request.app.state.store
        run_id, history = await store.begin_run(str(session_id), request.state.user_id)
        request.state.run_id, request.state.session_id = run_id, str(session_id)
        progress = RunProgress()
        progress.context.manual_compaction = True
        try:
            async with asyncio.timeout(settings.run_timeout_seconds):
                await request.app.state.context_manager.prepare(
                    history, len(history), str(session_id), run_id, progress, force=True
                )
            await store.complete_run(
                str(session_id),
                run_id,
                [],
                "历史压缩完成；原始对话和计算产物完整保留。",
                [],
                progress.usage,
                context=progress.context,
            )
        except BaseException:
            await store.fail_run(
                str(session_id),
                run_id,
                "context_compaction_failed",
                tool_calls=[],
                usage=progress.current_usage,
                context=progress.context,
            )
            raise
        return {
            "run_id": run_id,
            "context": progress.context.model_dump(),
            "memory": (await store.get_context_memory(str(session_id))).model_dump(),
        }

    @app.get("/runs/{run_id}", response_model=RunDetail, tags=["runs"])
    async def get_run(run_id: UUID, request: Request):
        return await request.app.state.store.get_run(str(run_id), request.state.user_id)

    @app.post("/chat/requests", status_code=202, tags=["agent"])
    async def enqueue_chat(body: QueuedChatRequest, request: Request, retry: bool = False):
        if calculation_enabled or (ssh_hosts and body.message.lstrip().startswith("/shell")):
            local_service(request)
        message = ChatRequest.model_validate(
            body.model_dump(mode="json", exclude={"request_id"}, exclude_unset=True)
        )
        return await request.app.state.chat_queue.enqueue(
            str(body.request_id), message, request, retry
        )

    @app.post("/sessions/{session_id}/questions/{question_id}/answer", status_code=202)
    async def answer_question(session_id: UUID, question_id: UUID, body: Reply, request: Request):
        if calculation_enabled:
            local_service(request)
        store = request.app.state.store
        async with store.lock:
            row = await store._one(
                "SELECT json_extract(call.value,'$.result.questions') AS questions "
                "FROM sessions s JOIN runs r ON r.session_id=s.id, json_each(r.tool_calls) call "
                "WHERE s.id=? AND s.owner_id IS ? AND r.status='completed' "
                "AND json_extract(call.value,'$.name')=? "
                "AND json_extract(call.value,'$.result.question_id')=?",
                (str(session_id), request.state.user_id, QUESTION_TOOL.name, str(question_id)),
            )
        if row is None:
            raise AgentError("question_not_found", "当前会话没有这项提问。", 404)
        answer = body.message(Ask(questions=json.loads(row["questions"])))
        return await request.app.state.chat_queue.enqueue(
            str(question_id),
            ChatRequest(session_id=session_id, message=answer),
            request,
            False,
            answering=str(question_id),
        )

    @app.get("/sessions/{session_id}/chat-requests", tags=["sessions"])
    async def pending_chat(session_id: UUID, request: Request, compact: bool = False):
        return await request.app.state.chat_queue.list(
            str(session_id), request.state.user_id, compact=compact
        )

    @app.get("/sessions/{session_id}/chat-requests/{request_id}/stream", tags=["sessions"])
    async def stream_answer(session_id: UUID, request_id: UUID, request: Request):
        store = request.app.state.store
        async with store.lock:
            row = await store._one(
                "SELECT r.status FROM chat_requests r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.id=? AND s.owner_id IS ?",
                (str(request_id), str(session_id), request.state.user_id),
            )
        if not row:
            raise AgentError("request_not_found", "消息不存在。", 404)
        preview = request.app.state.chat_queue.live.get(str(request_id))
        events = preview.events() if preview else iter(["event: settled\ndata: {}\n\n"])
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/chat", response_model=ChatResponse, tags=["agent"])
    async def chat(body: ChatRequest, request: Request):
        if calculation_enabled or (ssh_hosts and body.message.lstrip().startswith("/shell")):
            local_service(request)
        if body.structure_ids and not calculation_enabled:
            raise AgentError("automation_disabled", "结构附件需要本地自动化功能。", 422)
        store: Store = request.app.state.store
        provider: ProviderRouter = request.app.state.provider
        _, configuration = provider.configuration()
        saved_model = None
        if body.session_id:
            saved_model = await store.get_session_model(
                str(body.session_id),
                request.state.user_id,
                configuration.provider,
                configuration.base_url,
            )
        provider.select_model(body.model if "model" in body.model_fields_set else saved_model)
        binding = await request.app.state.agent_backends.binding(
            str(body.session_id) if body.session_id else None
        )
        group = await turn_members(store, str(body.session_id) if body.session_id else None)
        if group:
            local_service(request)
        elif binding["backend"] != "builtin":
            local_service(request)
            if not await request.app.state.agent_backends.ready(
                str(body.session_id) if body.session_id else None
            ):
                raise AgentError("agent_not_configured", "请在 Agent 设置中连接本地 Agent。", 503)
        elif not await provider.ready():
            raise AgentError("model_not_configured", "请在模型设置中登录 ChatGPT 或配置 API。", 503)
        file_context = await attachment_context(store, body.file_ids, request.state.user_id)
        session_id = (
            str(body.session_id)
            if body.session_id
            else (await store.create_session("新对话", request.state.user_id))["id"]
        )
        request.state.session_id = session_id
        attached_structures = [
            calculation_chat.structure_summary(
                calculation_chat.load(session_id, "structures", str(identifier))
            )
            for identifier in body.structure_ids
        ]
        selected_host = authorized_host(body.message, available_hosts())
        run_id, history = await store.begin_run(
            session_id,
            request.state.user_id,
            request_id=getattr(request.state, "chat_request_id", None),
            file_ids=tuple(body.file_ids),
        )
        request.state.run_id = run_id
        content = body.message
        context = "\n\n".join(part for part in (body.context, file_context) if part)
        if context:
            content += "\n\n<user_provided_reference>\n" + context + "\n</user_provided_reference>"
        if attached_structures:
            content += "\n\n已验证结构附件（用 ID 调用计算工具）：\n" + json.dumps(
                attached_structures, ensure_ascii=False
            )
        messages = [*history, {"role": "user", "content": content}]
        progress = RunProgress(preview=getattr(request.state, "live_answer", None))

        async def record_failure(code: str, *, cancelled=False):
            await store.fail_run(
                session_id,
                run_id,
                code,
                tool_calls=progress.tool_calls,
                usage=progress.current_usage,
                cancelled=cancelled,
                context=progress.context,
            )

        from chemistry_agent.automation.plan_authority import (
            authorized_queue,
            auto_queue,
            auto_request,
        )

        source_request = auto_request.set(getattr(request.state, "chat_request_id", run_id))
        config_token = configuration_grant.set(
            calculation_enabled and authorizes_configuration(body.message)
        )
        shell_token = shell_grant.set(selected_host)
        grant = auto_queue.set(authorized_queue(body.message) if calculation_enabled else None)
        try:
            async with asyncio.timeout(settings.run_timeout_seconds):
                router = request.app.state.agent_backends
                execute = partial(run_group, router, group) if group else router.run
                result = await execute(
                    messages,
                    progress,
                    session_id=session_id,
                    run_id=run_id,
                    history_count=len(history),
                )
            answer = result["messages"][-1]["content"]
            await store.complete_run(
                session_id,
                run_id,
                result["messages"][len(history) :],
                answer,
                result["tool_calls"],
                result["usage"],
                context=progress.context,
            )
        except TimeoutError as exc:
            await record_failure("run_timeout")
            raise AgentError("run_timeout", "任务运行超时，请缩小任务后重试。", 504) from exc
        except AgentError as exc:
            await record_failure(exc.code)
            raise
        except asyncio.CancelledError:
            await record_failure("run_cancelled", cancelled=True)
            raise
        except Exception as exc:
            await record_failure("internal_error")
            # Only log the exception type, never an upstream body, prompt, or credential.
            logger.error("Agent run failed (%s)", type(exc).__name__)
            raise AgentError("internal_error", "任务执行失败，请重试。", 500) from exc
        finally:
            configuration_grant.reset(config_token)
            shell_grant.reset(shell_token)
            auto_queue.reset(grant)
            auto_request.reset(source_request)
        return ChatResponse(
            session_id=session_id,
            run_id=run_id,
            answer=answer,
            tool_calls=result["tool_calls"],
            usage=result["usage"],
            context=progress.context,
        )

    return app
