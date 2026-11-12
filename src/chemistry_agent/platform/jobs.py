"""Agent-facing inspection of ordinary Slurm jobs in the current conversation."""

import base64
from typing import Literal
from uuid import UUID

from pydantic import Field

from chemistry_agent.automation.job_models import JobAdopt
from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.task_files import relative_path
from chemistry_agent.platform.contracts import OperationContext as ToolContext


class JobFileQuery(StrictModel):
    job_id: UUID
    mode: Literal["list", "read", "status"] = "list"
    path: str = "*"
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=16000, ge=1, le=64000)


class JobHosts(StrictModel):
    host: str | None = None


class JobInspect(StrictModel):
    job_id: UUID


class JobOperations:
    def __init__(self, plans) -> None:
        self.plans = plans

    async def status(self, args: JobInspect, context: ToolContext) -> dict:
        return await self.plans.slurm.inspect(context.session_id, str(args.job_id))

    async def adopt(self, args: JobAdopt, context: ToolContext) -> dict:
        plans = self.plans
        from chemistry_agent.automation.job_adoption import adopt_job

        return await adopt_job(plans, args, context)

    async def inspect(self, args: JobFileQuery, context: ToolContext) -> dict:
        plans = self.plans
        record = plans.slurm.load(context.session_id, str(args.job_id))
        if args.mode == "status":
            return await plans.slurm.inspect(context.session_id, str(args.job_id))
        name = relative_path(args.path)
        if args.mode == "list":
            return await plans.slurm.remote(record, "files", patterns=[name])
        result = await plans.slurm.remote(
            record, "read", path=name, offset=args.offset, length=args.length
        )
        data = base64.b64decode(result["data"])
        return {
            "path": name,
            "content": data.decode(errors="replace"),
            "size": result["size"],
            "next_offset": args.offset + len(data)
            if args.offset + len(data) < result["size"]
            else None,
        }

    async def hosts(self, args: JobHosts, context: ToolContext) -> dict:
        plans = self.plans
        from chemistry_agent.automation.local_ssh import discover_aliases

        if args.host:
            return await plans.slurm.request(args.host, {"root": ".", "action": "environment"})
        allowed = plans.slurm.hosts()
        return {
            "hosts": allowed,
            "local_ssh_config": [row for row in discover_aliases() if row["alias"] in allowed],
            "discovery": "Use remote_shell(host=alias, command=...) to inspect remote shell/config "
            "files and find executables/environment scripts/potential libraries; "
            "no AiiDA required.",
            "backend": "slurm",
            "requires_aiida": False,
            "input": "job.host, job.script, job.resources, job.files, job.retrieve",
            "files": "node.files: node/job_id/file_id, path, target; no file type whitelist",
        }
