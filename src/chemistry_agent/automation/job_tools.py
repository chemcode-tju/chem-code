"""Agent-facing inspection of ordinary Slurm jobs in the current conversation."""

import base64
from typing import Literal
from uuid import UUID

from pydantic import Field

from chemistry_agent.automation.job_models import JobAdopt
from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.task_files import relative_path
from chemistry_agent.tools import Tool, ToolContext


class JobFileQuery(StrictModel):
    job_id: UUID
    mode: Literal["list", "read", "status"] = "list"
    path: str = "*"
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=16000, ge=1, le=64000)


class JobHosts(StrictModel):
    host: str | None = None


def tools(plans) -> list[Tool]:
    async def adopt(args: JobAdopt, context: ToolContext) -> dict:
        from chemistry_agent.automation.job_adoption import adopt_job

        return await adopt_job(plans, args, context)

    async def inspect(args: JobFileQuery, context: ToolContext) -> dict:
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

    async def hosts(args: JobHosts, context: ToolContext) -> dict:
        if args.host:
            return await plans.slurm.request(args.host, {"root": ".", "action": "environment"})
        return {
            "hosts": plans.slurm.hosts(),
            "backend": "slurm",
            "requires_aiida": False,
            "input": "job.host, job.script, job.resources, job.files, job.retrieve",
            "files": "node.files: node/job_id/file_id, path, target; no file type whitelist",
        }

    return [
        Tool(
            "calculation_job_files",
            "List/read raw files or inspect a generic Slurm job. "
            "Use job_id from a plan runtime generic_job_id. Binary/large files can be "
            "passed to any downstream node using files references.",
            JobFileQuery,
            inspect,
            True,
        ),
        Tool(
            "calculation_job_hosts",
            "List registered SSH hosts for generic Slurm tasks. "
            "With host, query actual Slurm partitions/resources and account (read-only). "
            "No AiiDA profile or executable plugin is required.",
            JobHosts,
            hosts,
            True,
        ),
        Tool(
            "calculation_job_adopt",
            "Monitor an existing Slurm job submitted through a terminal. Supply host and "
            "scheduler_job_id; validates cluster, submission time and WorkDir. Does not submit "
            "or alter remote inputs. Repeated registration returns the existing binding. "
            "Optional plan_id attaches it to this conversation's active plan; otherwise creates "
            "a monitoring plan. On completion/failure this conversation agent wakes to process "
            "instruction. retrieve lists outputs to download; raw files remain readable. "
            "Use calculation_wait_register for grouped waits. Register concrete array elements.",
            JobAdopt,
            adopt,
            True,
        ),
    ]
