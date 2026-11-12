"""One operation surface for the UI, Python clients and any agent adapter."""

from uuid import UUID

from pydantic import Field

from chemistry_agent.automation.job_models import JobAdopt
from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.plan_models import NodeRepair, PlanDraft, PlanExtension, PlanQuery
from chemistry_agent.automation.plan_stop import PlanStop
from chemistry_agent.automation.wait_models import WaitControl, WaitRegister
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.artifacts import ArtifactOperations, ArtifactRead, ArtifactRegister
from chemistry_agent.platform.contracts import OperationContext
from chemistry_agent.platform.jobs import JobFileQuery, JobHosts, JobInspect, JobOperations
from chemistry_agent.platform.steps import StepReport, report_step
from chemistry_agent.platform.waits import WaitOperations


class PlanConfirmation(StrictModel):
    plan_id: UUID
    revision: int = Field(ge=1)


class PlanControl(StrictModel):
    plan_id: UUID
    action: str = Field(pattern="^(pause|resume)$")


class DeliverySelection(StrictModel):
    plan_id: UUID
    adapter: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ComputationPlatform:
    """The legacy executor is a backend; its records stay compatible during migration."""

    def __init__(self, plans) -> None:
        self.plans = plans
        self.records = plans.chat.records
        self.jobs = JobOperations(plans)
        self.waits = WaitOperations(plans)
        self.artifacts = ArtifactOperations(plans)
        self.operations = {
            "plan.save": (PlanDraft, plans.draft),
            "plan.get": (PlanQuery, plans.query),
            "plan.extend": (PlanExtension, plans.extend),
            "plan.repair": (NodeRepair, plans.repair_node),
            "plan.stop": (PlanStop, plans.stop),
            "plan.confirm": (PlanConfirmation, self.confirm),
            "plan.control": (PlanControl, self.control),
            "plan.delivery": (DeliverySelection, self.delivery),
            "plan.report": (StepReport, self.report),
            "artifact.register": (ArtifactRegister, self.artifacts.register),
            "artifact.read": (ArtifactRead, self.artifacts.read),
            "job.adopt": (JobAdopt, self.jobs.adopt),
            "job.files": (JobFileQuery, self.jobs.inspect),
            "job.inspect": (JobInspect, self.jobs.status),
            "job.hosts": (JobHosts, self.jobs.hosts),
            "wait.register": (WaitRegister, self.waits.register),
            "wait.control": (WaitControl, self.waits.control),
        }

    def handler(self, operation: str):
        if operation not in self.operations:
            raise AgentError("platform_operation_unknown", "未知的平台操作。", 404)
        return self.operations[operation][1]

    async def execute(self, operation: str, arguments: dict, context: OperationContext) -> dict:
        handler = self.handler(operation)
        model = self.operations[operation][0]
        return await handler(model.model_validate(arguments), context)

    def catalog(self) -> dict:
        return {name: model.model_json_schema() for name, (model, _) in self.operations.items()}

    async def confirm(self, args: PlanConfirmation, context: OperationContext) -> dict:
        return await self.plans.confirm(context.session_id, str(args.plan_id), args.revision)

    async def control(self, args: PlanControl, context: OperationContext) -> dict:
        return await self.plans.control(context.session_id, str(args.plan_id), args.action)

    async def report(self, args: StepReport, context: OperationContext) -> dict:
        return await report_step(self.plans, args, context)

    async def delivery(self, args: DeliverySelection, context: OperationContext) -> dict:
        key = (context.session_id, str(args.plan_id))
        async with self.plans.lock(*key):
            running = False
            if self.plans.store:
                async with self.plans.store.lock:
                    running = await self.plans.store._one(
                        "SELECT id FROM agent_events WHERE session_id=? AND plan_id=? "
                        "AND status='running' LIMIT 1",
                        key,
                    )
            if running or key in self.plans.dispatcher.tasks:
                raise AgentError("delivery_busy", "事件正在处理，请在本轮结束后切换。", 409)
            plan = self.records.load(key[0], "plans", key[1])
            plan["delivery_adapter"] = args.adapter
            self.plans.save(key[0], plan)
        return {
            "plan_id": key[1],
            "adapter": args.adapter,
            "available": args.adapter in self.plans.dispatcher.adapters,
        }
