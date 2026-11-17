from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from chemistry_agent.automation.configuration import (
    CalculationConfiguration,
)
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.errors import AgentError

router = APIRouter(tags=["automation"])
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def local_service(request: Request) -> AutomationService:
    """Protect local SSH and calculation state from shared or remote clients."""
    if request.app.state.settings.auth_enabled:
        raise AgentError("automation_local_only", "此版本自动化仅用于本地单用户部署。", 403)
    if (
        request.url.hostname not in LOCAL_HOSTS
        or not request.client
        or request.client.host not in LOCAL_HOSTS
    ):
        raise AgentError("automation_local_only", "自动化接口仅允许本机访问。", 403)
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise AgentError("origin_rejected", "请求来源不被允许。", 403)
    return request.app.state.automation


@router.get("/api/automation/configuration")
async def configuration(service: Annotated[AutomationService, Depends(local_service)]) -> dict:
    return CalculationConfiguration(service.settings.platform_path).read()


@router.get("/automation", include_in_schema=False)
async def automation_page() -> RedirectResponse:
    return RedirectResponse("/settings#automation", status_code=307)


@router.get("/api/automation/notifications")
async def notifications(request: Request, limit: int = Query(40, ge=1, le=10000)) -> dict:
    local_service(request)
    monitor = getattr(request.app.state, "task_monitor", None)
    return (
        monitor.events.notification_inbox(limit)
        if monitor
        else {"events": [], "unread": 0, "monitor": {}}
    )


@router.post("/api/automation/notifications/read-all")
async def read_all_notifications(request: Request) -> dict:
    local_service(request)
    if monitor := getattr(request.app.state, "task_monitor", None):
        monitor.events.mark_read()
    return {"read": True}


@router.post("/api/automation/notifications/{event_id}/read")
async def read_notification(event_id: UUID, request: Request) -> dict:
    local_service(request)
    monitor = getattr(request.app.state, "task_monitor", None)
    if monitor:
        monitor.events.mark_read(str(event_id))
    return {"read": True}


SettingsService = Annotated[AutomationService, Depends(local_service)]
