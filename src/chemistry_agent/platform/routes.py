"""Local platform API, independent of chat prompts or a configured model."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import Field, ValidationError

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.plan_authority import auto_request
from chemistry_agent.automation.routes import local_service
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import OperationContext

router = APIRouter(prefix="/api/platform", tags=["computation platform"])


class OperationRequest(StrictModel):
    arguments: dict = Field(default_factory=dict)


def platform(request: Request):
    local_service(request)
    if not request.app.state.settings.automation_enabled:
        raise AgentError("automation_disabled", "请启用计算平台。", 503)
    return request.app.state.platform


@router.get("/operations")
async def catalog(request: Request) -> dict:
    return {"operations": platform(request).catalog()}


@router.post("/{session_id}/operations/{operation}")
async def execute(
    session_id: UUID, operation: str, body: OperationRequest, request: Request
) -> dict:
    service = platform(request)
    await request.app.state.store.get_session(str(session_id), request.state.user_id)
    context = OperationContext(str(session_id), str(uuid4()))
    # HTTP confirmation/stop is an explicit user action; it does not grant autonomous repair.
    token = auto_request.set(context.run_id)
    try:
        return await service.execute(operation, body.arguments, context)
    except ValidationError as exc:
        raise AgentError("platform_input_invalid", str(exc), 422) from exc
    finally:
        auto_request.reset(token)


@router.get("/{session_id}/records")
async def records(session_id: UUID, request: Request) -> dict:
    service = platform(request)
    await request.app.state.store.get_session(str(session_id), request.state.user_id)
    return {
        "plans": service.plans.records(str(session_id)),
        "jobs": service.records.list_records(str(session_id), "slurm_jobs"),
        "artifacts": service.records.list_records(str(session_id), "artifacts"),
    }
