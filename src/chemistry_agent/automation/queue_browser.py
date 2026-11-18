"""Read-only browser access to the current cluster account's Slurm jobs."""

import base64

from fastapi import APIRouter, Path, Query, Request, Response

from chemistry_agent.automation.queue_remote import JOB_ID
from chemistry_agent.environments.registry import Environments
from chemistry_agent.file_browser import job_file_operation
from chemistry_agent.file_browser import job_scheduler as slurm

router = APIRouter(prefix="/api/job-monitor", tags=["queue monitor"])


@router.get("/hosts")
async def hosts(request: Request) -> dict:
    jobs = slurm(request)
    enabled = set(jobs.hosts())
    inventory = Environments(jobs.root).inventory(jobs.settings.allowed_ssh_hosts)
    return {
        "hosts": [
            {"id": row["id"], "name": row["name"]} for row in inventory if row["id"] in enabled
        ]
    }


@router.get("/{host}/jobs")
async def jobs(host: str, request: Request, history: bool = False) -> dict:
    return await slurm(request).request(host, {"action": "queue_history" if history else "queue"})


@router.get("/{host}/jobs/{job_id}/metadata")
async def metadata(
    host: str,
    request: Request,
    job_id: str = Path(pattern=JOB_ID, max_length=64),
    submitted: str = Query(max_length=80),
    path: str = Query(max_length=4096),
    history: bool = False,
) -> dict:
    return await job_file_operation(
        request,
        host,
        {
            "action": "queue_metadata",
            "job_id": job_id,
            "submitted": submitted,
            "path": path,
            "history": history,
        },
    )


@router.get("/{host}/jobs/{job_id}/latest-frame")
async def latest_frame(
    host: str,
    request: Request,
    job_id: str = Path(pattern=JOB_ID, max_length=64),
    submitted: str = Query(max_length=80),
    path: str = Query(max_length=4096),
    history: bool = False,
) -> Response:
    result = await job_file_operation(
        request,
        host,
        {
            "action": "queue_dump_frame",
            "job_id": job_id,
            "submitted": submitted,
            "path": path,
            "history": history,
        },
    )
    return Response(
        base64.b64decode(result["data"], validate=True),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"},
    )
