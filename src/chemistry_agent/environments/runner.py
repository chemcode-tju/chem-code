"""Small authenticated runner. Slurm remains authoritative; no LLM or polling loop.

Use CHEM_RUNNER_TOKEN and CHEM_RUNNER_STATE with uvicorn factory mode. Run the
platform (including its monitor and Agent) on a persistent host for unattended work.
"""

import asyncio
import hashlib
import hmac
import json
import os
import platform
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from filelock import FileLock, Timeout

from chemistry_agent.automation.ssh_profiles import write_private


class RunnerOperations:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def execute(self, operation: str, payload: dict) -> dict:
        if operation == "probe":
            return {
                "name": platform.node(),
                "platform": platform.system(),
                "cpu_count": os.cpu_count(),
                "executables": {
                    n: shutil.which(n)
                    for n in ("sbatch", "squeue", "sacct", "python3", "codex", "agy")
                },
                "protocol": 1,
            }
        if operation == "slurm":
            from chemistry_agent.automation.queue_remote import (
                QUEUE_ACTIONS,
                job_root,
                queue_operation,
            )
            from chemistry_agent.automation.slurm_remote import dispatch
            from chemistry_agent.errors import AgentError

            if payload.get("action") in QUEUE_ACTIONS:
                try:
                    if payload["action"] == "queue_browse":
                        from chemistry_agent.file_browser import read_job_on_runner

                        directory = await asyncio.to_thread(job_root, payload)
                        return await read_job_on_runner(directory, payload)
                    return await asyncio.to_thread(queue_operation, payload)
                except (AgentError, ValueError, OSError, RuntimeError) as exc:
                    return {"error": str(exc)}

            return await asyncio.to_thread(dispatch, payload)
        if operation == "shell":
            return await self.shell(payload)
        if operation == "shell_status":
            path = self.root / "commands" / f"{UUID(payload['id'])}.json"
            return json.loads(path.read_text())
        raise ValueError("Unknown runner operation")

    async def shell(self, payload: dict) -> dict:
        """Write intent before launch. Unknown interruption is never blindly replayed."""
        identifier = str(UUID(payload["id"]))
        command = str(payload["command"])
        directory = await asyncio.to_thread(Path(payload.get("cwd") or "~").expanduser)
        fingerprint = hashlib.sha256(
            json.dumps({"command": command, "cwd": str(directory)}, sort_keys=True).encode()
        ).hexdigest()
        path = self.root / "commands" / f"{identifier}.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = FileLock(str(path.with_suffix(".lock")))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return {"id": identifier, "state": "running", "replayed": False}
        try:
            if path.exists():
                record = json.loads(path.read_text())
                if record["fingerprint"] != fingerprint:
                    raise ValueError("Request ID already belongs to a different command")
                if record["state"] == "running":
                    record["state"] = "unknown"
                    record["message"] = "进程曾中断，请核对目录或 Slurm 作业后再决定下一步。"
                return record | {"replayed": False}
            record = {"id": identifier, "fingerprint": fingerprint, "state": "running"}
            write_private(path, record)
            process = None
            try:
                with (
                    path.with_suffix(".stdout").open("wb") as stdout,
                    path.with_suffix(".stderr").open("wb") as stderr,
                ):
                    process = await asyncio.create_subprocess_exec(
                        "bash",
                        "-c",
                        command,
                        cwd=directory,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    async with asyncio.timeout(60):
                        code = await process.wait()
                record.update(state="completed", exit_code=code, ok=code == 0)
            except BaseException:
                record.update(state="unknown", message="命令未正常返回，请先核对副作用。")
                raise
            finally:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                for name in ("stdout", "stderr"):
                    output = path.with_suffix(f".{name}")
                    if output.exists():
                        with output.open("rb") as stream:
                            record[name] = stream.read(16384).decode(errors="replace")
                        record[f"{name}_path"] = str(output)
                write_private(path, record)
            return record
        finally:
            lock.release()


def create_runner(root: Path | None = None, token: str | None = None) -> FastAPI:
    """Fail closed: the service never starts with an empty credential."""
    root = (
        root or Path(os.environ.get("CHEM_RUNNER_STATE", "~/.local/state/chem-runner")).expanduser()
    )
    token = token or os.environ.get("CHEM_RUNNER_TOKEN", "")
    if len(token) < 24:
        raise ValueError("CHEM_RUNNER_TOKEN must contain at least 24 characters")
    operations = RunnerOperations(root)
    app = FastAPI(title="Chem Code Runner", docs_url=None, redoc_url=None)

    @app.post("/v1/{operation}")
    async def execute(operation: str, request: Request):
        if request.headers.get("origin") or not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            payload = await request.json()
            return await operations.execute(operation, payload)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=422)
        except TimeoutError:
            return JSONResponse(
                {"error": "Command state unknown; inspect before retry"}, status_code=504
            )

    return app
