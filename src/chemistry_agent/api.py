import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from chemistry_agent import __version__
from chemistry_agent.agent import AUTOMATION_PROMPT, SYSTEM_PROMPT, ChemistryAgent, RunProgress
from chemistry_agent.auth import COOKIE_NAME, AuthenticationMiddleware, AuthManager
from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.chat_routes import router as calculation_chat_router
from chemistry_agent.automation.monitor import TaskMonitor
from chemistry_agent.automation.routes import local_service
from chemistry_agent.automation.routes import router as automation_router
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.config import Settings
from chemistry_agent.context import ContextManager
from chemistry_agent.errors import AgentError
from chemistry_agent.model_routes import router as model_router
from chemistry_agent.model_runtime import ProviderRouter
from chemistry_agent.provider import ToolCall
from chemistry_agent.schemas import (
    ChatRequest,
    ChatResponse,
    RunDetail,
    SessionCreate,
    SessionDetail,
    SessionSummary,
    StrictModel,
    ToolExecution,
)
from chemistry_agent.store import Store
from chemistry_agent.tokens import TokenCounter
from chemistry_agent.tools import ToolRegistry

logger = logging.getLogger(__name__)
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"


class ToolRequest(StrictModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings()
    automation = AutomationService(settings)
    calculation_chat = CalculationChat(automation)
    calculation_enabled = settings.automation_enabled and not settings.auth_enabled
    registry = ToolRegistry(calculation_chat.tools() if calculation_enabled else [])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = await Store.open(settings.database_path)
        try:
            provider = ProviderRouter(settings, transport=transport)
            try:
                app.state.provider = provider
                app.state.store = store
                app.state.auth = AuthManager(settings, store)
                counter = await asyncio.to_thread(TokenCounter, settings.tokenizer_path)
                context_manager = ContextManager(
                    settings,
                    provider,
                    store,
                    counter,
                    SYSTEM_PROMPT + (AUTOMATION_PROMPT if calculation_enabled else ""),
                    registry.schemas(),
                )
                app.state.agent = ChemistryAgent(
                    provider,
                    registry,
                    settings.max_tool_rounds,
                    context_manager,
                )
                app.state.context_manager = context_manager
                monitor = (
                    TaskMonitor(calculation_chat, provider)
                    if calculation_enabled and settings.automation_monitor_enabled
                    else None
                )
                app.state.task_monitor = monitor
                calculation_chat.monitor = monitor
                if monitor:
                    monitor.start()
                try:
                    yield
                finally:
                    if monitor:
                        await monitor.close()
            finally:
                await provider.close()
        finally:
            await store.close()

    app = FastAPI(
        title="Chemistry Agent",
        version=__version__,
        lifespan=lifespan,
        description="化学研究助手：ChatGPT 账号登录或自选模型 API + LangGraph + RDKit。",
    )
    app.mount("/assets", StaticFiles(directory=WEB_DIRECTORY), name="assets")
    app.include_router(model_router)

    app.state.settings = settings
    app.state.automation = automation
    app.state.calculation_chat = calculation_chat
    app.include_router(automation_router)
    app.include_router(calculation_chat_router)
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
    async def index():
        return FileResponse(WEB_DIRECTORY / "index.html")

    @app.get("/health", tags=["service"])
    async def health(request: Request):
        provider = request.app.state.provider
        _, configuration = provider.configuration()
        ready = (
            False if settings.auth_enabled and not request.state.user else await provider.ready()
        )
        return {
            "status": "ok",
            "version": __version__,
            "model_configured": ready,
            "model": configuration.model or "ChatGPT",
            "provider": configuration.provider,
            "thinking": configuration.thinking,
            "context_window_tokens": settings.context_window_tokens,
            "context_input_budget_tokens": settings.context_input_budget,
            "context_recent_turns": settings.context_recent_turns,
            "automation_enabled": calculation_enabled,
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
        return await request.app.state.store.create_session(body.title, request.state.user_id)

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
    ):
        return await request.app.state.store.list_runs(
            str(session_id), limit, offset, request.state.user_id
        )

    @app.get("/sessions/{session_id}/context", tags=["sessions"])
    async def context_status(session_id: UUID, request: Request):
        store = request.app.state.store
        session = await store.get_session(str(session_id), request.state.user_id)
        memory = await store.get_context_memory(str(session_id))
        manager = request.app.state.context_manager
        wire, compacted = manager._wire(session["messages"], [], memory)
        return {
            **memory.model_dump(),
            "original_messages": len(session["messages"]),
            "estimated_input_tokens": manager.counter.request_tokens(wire, manager.tools),
            "input_budget_tokens": settings.context_input_budget,
            "trigger_tokens": int(settings.context_input_budget * settings.context_trigger_ratio),
            "recent_turns": settings.context_recent_turns,
            "compacted_tool_results": compacted,
            "tokenizer": manager.counter.method,
        }

    @app.post("/sessions/{session_id}/context/compact", tags=["sessions"])
    async def compact_context(session_id: UUID, request: Request):
        if calculation_enabled:
            local_service(request)
        if not await request.app.state.provider.ready():
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
                usage=progress.usage,
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

    @app.post("/chat", response_model=ChatResponse, tags=["agent"])
    async def chat(body: ChatRequest, request: Request):
        if calculation_enabled:
            local_service(request)
        if body.structure_ids and not calculation_enabled:
            raise AgentError("automation_disabled", "结构附件需要本地自动化功能。", 422)
        if not await request.app.state.provider.ready():
            raise AgentError("model_not_configured", "请在模型设置中登录 ChatGPT 或配置 API。", 503)
        store: Store = request.app.state.store
        session_id = (
            str(body.session_id)
            if body.session_id
            else (await store.create_session(body.message[:80], request.state.user_id))["id"]
        )
        request.state.session_id = session_id
        attached_structures = [
            calculation_chat.structure_summary(
                calculation_chat.load(session_id, "structures", str(identifier))
            )
            for identifier in body.structure_ids
        ]
        run_id, history = await store.begin_run(session_id, request.state.user_id)
        request.state.run_id = run_id
        content = body.message
        if body.context:
            content += (
                "\n\n<user_provided_reference>\n" + body.context + ("\n</user_provided_reference>")
            )
        if attached_structures:
            content += "\n\n已验证结构附件（用 ID 调用计算工具）：\n" + json.dumps(
                attached_structures, ensure_ascii=False
            )
        messages = [*history, {"role": "user", "content": content}]
        progress = RunProgress()

        async def record_failure(code: str, *, cancelled=False):
            await store.fail_run(
                session_id,
                run_id,
                code,
                tool_calls=progress.tool_calls,
                usage=progress.usage,
                cancelled=cancelled,
                context=progress.context,
            )

        try:
            async with asyncio.timeout(settings.run_timeout_seconds):
                result = await request.app.state.agent.run(
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
        return ChatResponse(
            session_id=session_id,
            run_id=run_id,
            answer=answer,
            tool_calls=result["tool_calls"],
            usage=result["usage"],
            context=progress.context,
        )

    return app
