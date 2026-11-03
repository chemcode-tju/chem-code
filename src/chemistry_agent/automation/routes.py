from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from chemistry_agent.automation.configuration import (
    CalculationConfiguration,
    CalculationProtocol,
    ExecutionTarget,
    PotentialPath,
    PotentialSource,
)
from chemistry_agent.automation.models import (
    CodeCreate,
    PreviewRequest,
    ServerCreate,
    SubmitRequest,
)
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.errors import AgentError

router = APIRouter(tags=["automation"])
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def local_service(request: Request) -> AutomationService:
    """The first version owns one local AiiDA profile; do not expose it to shared users."""
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
    return CalculationConfiguration(service.settings.aiida_path).read()


@router.post("/api/automation/potential-sources")
async def save_source(
    body: PotentialSource, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call(
        "save_configuration", {"kind": "sources", "value": body.model_dump(mode="json")}
    )


@router.post("/api/automation/protocols")
async def save_protocol(
    body: CalculationProtocol, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call(
        "save_configuration", {"kind": "protocols", "value": body.model_dump(mode="json")}
    )


@router.get("/automation", include_in_schema=False)
async def automation_page() -> RedirectResponse:
    return RedirectResponse("/settings#automation", status_code=307)


@router.get("/api/automation/status")
async def status(service: Annotated[AutomationService, Depends(local_service)]) -> dict:
    return await service.call("status")


@router.post("/api/automation/servers")
async def create_server(
    body: ServerCreate, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call("create_server", body.model_dump(mode="json"))


@router.post("/api/automation/servers/{server_id}/test")
async def test_server(
    server_id: UUID, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call("test_server", {"server_id": str(server_id)})


@router.post("/api/automation/previews")
async def preview(
    body: PreviewRequest, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call("preview", body.model_dump(mode="json"))


@router.post("/api/automation/submissions")
async def submit(
    body: SubmitRequest, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call("submit", body.model_dump(mode="json"))


@router.get("/api/automation/jobs")
async def jobs(service: Annotated[AutomationService, Depends(local_service)]) -> dict:
    return await service.call("jobs")


@router.get("/api/automation/jobs/{job_id}")
async def job_details(
    job_id: UUID, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call("job_details", {"job_id": str(job_id)})


@router.post("/api/automation/codes")
async def create_code(
    body: CodeCreate, service: Annotated[AutomationService, Depends(local_service)]
) -> dict:
    return await service.call("create_code", body.model_dump(mode="json"))


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


@router.get("/api/automation/settings")
async def automation_settings(service: SettingsService, server_id: UUID | None = None) -> dict:
    return await service.call("settings_status", {"server_id": str(server_id)} if server_id else {})


@router.post("/api/automation/settings/initialize")
async def initialize(service: SettingsService) -> dict:
    return await service.call("settings_initialize")


@router.post("/api/automation/settings/potential-sources")
async def register_source(body: PotentialPath, service: SettingsService) -> dict:
    return await service.call("register_potential_path", body.model_dump(mode="json"))


@router.post("/api/automation/settings/execution-targets")
async def register_execution_target(body: ExecutionTarget, service: SettingsService) -> dict:
    return await service.call("save_execution_target", body.model_dump(mode="json"))


@router.get("/api/automation/settings/servers/{server_id}/queues")
async def server_queues(server_id: UUID, service: SettingsService) -> dict:
    return await service.call("scheduler_options", {"server_id": str(server_id)})
