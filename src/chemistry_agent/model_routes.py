"""Model settings endpoints, protected by workspace authentication and origin checks."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ProviderRouter
from chemistry_agent.model_settings import ModelSettings
from chemistry_agent.schemas import SessionModelUpdate


def protect_settings(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise AgentError("origin_rejected", "请求来源不被允许。", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise AgentError("origin_rejected", "请求来源不被允许。", 403)
    if not request.app.state.provider.settings.auth_enabled:
        local = {"localhost", "127.0.0.1", "::1", "testserver", "testclient"}
        if (
            request.url.hostname not in local
            or not request.client
            or request.client.host not in local
        ):
            raise AgentError(
                "local_settings_only", "远程使用模型设置前，请启用工作空间账号登录。", 403
            )


router = APIRouter(tags=["model settings"], dependencies=[Depends(protect_settings)])


class ChatGPTLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["browser", "device"] = "browser"
    account_id: str | None = Field(default=None, min_length=1, max_length=160)
    new_account: bool = False


class ChatGPTAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(min_length=1, max_length=160)


@router.get("/api/model/models")
async def list_models(request: Request) -> dict:
    return await request.app.state.provider.list_models()


@router.get("/sessions/{session_id}/model")
async def read_session_model(session_id: UUID, request: Request) -> dict:
    _, configuration = request.app.state.provider.configuration()
    model = await request.app.state.store.get_session_model(
        str(session_id), request.state.user_id, configuration.provider, configuration.base_url
    )
    return {"provider": configuration.provider, "model": model}


@router.put("/sessions/{session_id}/model")
async def save_session_model(session_id: UUID, body: SessionModelUpdate, request: Request) -> dict:
    _, configuration = request.app.state.provider.configuration()
    await request.app.state.store.set_session_model(
        str(session_id),
        request.state.user_id,
        configuration.provider,
        configuration.base_url,
        body.model,
    )
    return {"provider": configuration.provider, "model": body.model}


@router.get("/settings", include_in_schema=False)
async def settings_page() -> FileResponse:
    from pathlib import Path

    return FileResponse(Path(__file__).parent / "web" / "settings.html")


@router.get("/api/model/settings")
async def read_settings(request: Request) -> dict:
    provider: ProviderRouter = request.app.state.provider
    return provider.store.load(request.state.user_id).public()


@router.put("/api/model/settings")
async def save_settings(body: ModelSettings, request: Request) -> dict:
    return request.app.state.provider.store.save(request.state.user_id, body).public()


@router.delete("/api/model/settings/key", status_code=204)
async def clear_key(request: Request) -> None:
    request.app.state.provider.store.clear_key(request.state.user_id)


@router.post("/api/model/test")
async def test_model(request: Request) -> dict:
    provider: ProviderRouter = request.app.state.provider
    completion = await provider.complete(
        [{"role": "user", "content": "这是连接测试，请只回复 OK，不要调用任何工具。"}],
        [],
        allow_tools=False,
        max_tokens=256,
        thinking=False,
    )
    if completion.message.tool_calls:
        raise AgentError("provider_invalid_response", "连接测试返回了意外工具调用。", 502)
    return {
        "ok": True,
        "answer": completion.message.content,
        "usage": completion.usage.model_dump(),
    }


@router.get("/api/model/chatgpt/status")
async def chatgpt_status(request: Request) -> dict:
    return await request.app.state.provider.chatgpt.status(request.state.user_id)


@router.post("/api/model/chatgpt/login")
async def chatgpt_login(body: ChatGPTLogin, request: Request) -> dict:
    return await request.app.state.provider.chatgpt.start_login(
        request.state.user_id, body.method, account_id=body.account_id, new_account=body.new_account
    )


@router.post("/api/model/chatgpt/account")
async def select_chatgpt_account(body: ChatGPTAccount, request: Request) -> dict:
    await request.app.state.provider.chatgpt.select_account(request.state.user_id, body.account_id)
    return {"active_account": body.account_id}


@router.post("/api/model/chatgpt/cancel", status_code=204)
async def cancel_login(request: Request) -> None:
    await request.app.state.provider.chatgpt.cancel_login(request.state.user_id)


@router.post("/api/model/chatgpt/logout")
async def chatgpt_logout(request: Request) -> dict:
    return await request.app.state.provider.chatgpt.logout(request.state.user_id)
