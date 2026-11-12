"""Agent tool descriptions; all operations delegate to the computation platform."""

from chemistry_agent.automation.job_models import JobAdopt
from chemistry_agent.platform.jobs import JobFileQuery as JobFileQuery
from chemistry_agent.platform.jobs import JobHosts as JobHosts
from chemistry_agent.tools import Tool


def tools(plans) -> list[Tool]:
    return [
        Tool(
            "calculation_job_files",
            "List/read raw files or inspect a generic Slurm job. "
            "Use job_id from a plan runtime generic_job_id. Binary/large files can be "
            "passed to any downstream node using files references.",
            JobFileQuery,
            plans.platform.handler("job.files"),
            True,
        ),
        Tool(
            "calculation_job_hosts",
            "List registered SSH hosts for generic Slurm tasks. "
            "With host, query actual Slurm partitions/resources and account (read-only). "
            "No AiiDA profile or executable plugin is required.",
            JobHosts,
            plans.platform.handler("job.hosts"),
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
            "Use step_id to attach many jobs/attempts to one agent-owned task. "
            "Use calculation_wait_register for grouped waits. Register concrete array elements.",
            JobAdopt,
            plans.platform.handler("job.adopt"),
            True,
        ),
    ]
