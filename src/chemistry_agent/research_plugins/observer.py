"""Read existing Slurm jobs; never submit, cancel, or infer success from disappearance."""

import asyncio
import shlex

from chemistry_agent.research_plugins.ledger import MonitorLedger


async def scheduler_read(host: str, arguments: list[str], local_host: str = "") -> str:
    """Query the local scheduler directly, or use an explicitly enabled SSH host."""
    command = (
        arguments
        if host == local_host
        else [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            host,
            shlex.join(arguments),
        ]
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(stderr.decode(errors="replace")[:1000] or "SSH/Slurm 查询失败")
    return stdout.decode(errors="replace")


def scheduler_row(text: str, job_id: str) -> tuple[str, str | None]:
    """Only the exact job/allocation counts; batch/extern rows are not its outcome."""
    for line in text.splitlines():
        fields = line.strip().split("|")
        if len(fields) >= 2 and fields[0] == job_id:
            return fields[1].split()[0].rstrip("+"), fields[2] if len(fields) > 2 else None
    return "UNKNOWN", None


async def observe_slurm(
    ledger: MonitorLedger, hosts: tuple[str, ...], local_host: str = ""
) -> None:
    for job in ledger.jobs():
        if job["backend"] != "slurm" or job["terminal"]:
            continue
        try:
            if job["host"] not in hosts and job["host"] != local_host:
                raise ValueError("此主机未在独立监控服务中启用")
            identifier = job["scheduler_id"]  # Validated digits and optional array index.
            text = await scheduler_read(
                job["host"], ["squeue", "-h", "-j", identifier, "-o", "%i|%T"], local_host
            )
            state, code = scheduler_row(text, identifier)
            if state == "UNKNOWN":
                text = await scheduler_read(
                    job["host"],
                    ["sacct", "-n", "-P", "-j", identifier, "--format=JobIDRaw,State,ExitCode"],
                    local_host,
                )
                state, code = scheduler_row(text, identifier)
            ledger.observe(job["id"], state, code)
        except (OSError, ValueError, TimeoutError) as error:
            ledger.observe(job["id"], "UNKNOWN", error=str(error))
