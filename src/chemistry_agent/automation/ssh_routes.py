"""Local-only SSH setup. Trust and credential operations are exclusively human-facing."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from chemistry_agent.automation.models import ServerCreate, StrictModel
from chemistry_agent.automation.routes import local_service
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.automation.ssh_profiles import load_profile
from chemistry_agent.errors import AgentError

router = APIRouter(prefix="/api/automation", tags=["SSH"])
Local = Annotated[AutomationService, Depends(local_service)]


class TrustHost(StrictModel):
    server: ServerCreate
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    previous_fingerprint: str | None = Field(default=None, pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=1024)


def require_automation(service: AutomationService) -> None:
    if not service.settings.automation_enabled:
        raise AgentError("automation_disabled", "请打开本地自动化工作区。", 503)


@router.post("/ssh/probe")
async def probe(body: ServerCreate, service: Local) -> dict:
    require_automation(service)
    from chemistry_agent.automation.ssh_connection import discover

    _, _, result = await discover(service.settings.aiida_path, body)
    return result


@router.post("/ssh/trust")
async def trust(body: TrustHost, service: Local) -> dict:
    require_automation(service)
    from chemistry_agent.automation.ssh_connection import pin_server

    _, result = await pin_server(
        service.settings.aiida_path, body.server, body.fingerprint, body.previous_fingerprint
    )
    return result


async def change_password(identifier: UUID, value: str | None, service: AutomationService) -> dict:
    require_automation(service)
    from chemistry_agent.automation.ssh_keychain import set_password

    profile = load_profile(service.settings.aiida_path, str(identifier))
    if profile["auth_mode"] != "password":
        raise AgentError("password_not_used", "此服务器使用密钥或 SSH 别名。", 422)
    await asyncio.to_thread(set_password, str(identifier), value)
    return {"stored": value is not None}


@router.put("/servers/{server_id}/password")
async def save_password(server_id: UUID, body: PasswordInput, service: Local) -> dict:
    return await change_password(server_id, body.password.get_secret_value(), service)


@router.delete("/servers/{server_id}/password")
async def remove_password(server_id: UUID, service: Local) -> dict:
    return await change_password(server_id, None, service)
