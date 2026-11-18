"""Fenced observer placement; existing job receipts and wait-event IDs stay authoritative."""

import asyncio
import json
import shlex
import time
from pathlib import Path

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting import monitor_worker
from chemistry_agent.hosting.connection import connection
from chemistry_agent.hosting.registry import Hosting

WORKER = Path(monitor_worker.__file__).read_text()


class MonitorPlacement:
    def __init__(self, jobs) -> None:
        self.jobs = jobs
        self.hosting = Hosting(jobs.root, jobs.settings.allowed_ssh_hosts)

    async def preference(self, session: str) -> str:
        store = getattr(getattr(self.jobs.chat, "plans", None), "store", None)
        if not store:
            return "local"
        async with store.lock:
            # Standalone monitor installations may have no Agent tables yet.
            table = await store._one(
                "SELECT name FROM sqlite_master WHERE name='agent_bindings'", ()
            )
            row = (
                await store._one("SELECT config FROM agent_bindings WHERE session_id=?", (session,))
                if table
                else None
            )
        config = json.loads(row["config"]) if row else {}
        selected = config.get("monitor_host", "auto")
        return config.get("host_id", "local") if selected == "auto" else selected

    def request(self, record: dict) -> dict:
        value = {"root": record["remote_directory"], "action": "status"}
        if record.get("origin") == "adopted":
            value.update(
                action="observe_external",
                scheduler_job_id=record["scheduler_job_id"],
                identity=record["identity"],
            )
        return value

    async def call(self, host: str, record: dict, generation: int, operation: str) -> dict:
        from chemistry_agent.automation.slurm_jobs import REMOTE_HELPER

        target = self.hosting.environments.get(record["host"])
        if target and target.transport == "runner":
            raise AgentError(
                "monitor_route_unavailable", "此作业通过 HTTP Runner 执行，请使用本机监控。", 409
            )
        alias = (target.ssh_alias or target.id) if target else record["host"]
        own = self.hosting.host(host)
        same = host == record["host"] or alias == (own.get("ssh_alias") or own["id"])
        payload = {
            "operation": operation,
            "key": record["id"],
            "generation": generation,
            "directory": f"~/.local/state/chem-code/{self.hosting.installation()}/monitor",
            "request": self.request(record),
            "target": None if same else alias,
        }
        if operation in {"prepare", "enable"}:
            payload.update(reader=REMOTE_HELPER, worker=WORKER)
        async with connection(self.hosting, host) as ssh:
            async with asyncio.timeout(60):
                result = await ssh.run(
                    "python3 -u -c " + shlex.quote(WORKER), input=json.dumps(payload), check=False
                )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise AgentError("monitor_connection", "远端监控未返回有效记录。", 503) from exc
        if data.get("error") or data.get("fenced") or result.exit_status:
            raise AgentError("monitor_handoff", data.get("error") or "监控所有权已变更。", 409)
        return data

    async def inspect(self, session: str, record: dict) -> dict | None:
        desired = await self.preference(session)
        owner = record.setdefault("monitor", {"host": "local", "generation": 0})
        if desired != owner["host"] or owner.get("handoff"):
            transition = owner.get("handoff") or {
                "host": desired,
                "generation": owner["generation"] + 1,
            }
            # Persist before contacting either host. Recovery resumes this same generation.
            owner["handoff"] = transition
            self.jobs.save(session, record)
            target, generation = transition["host"], transition["generation"]
            try:
                if target != "local":
                    await self.call(target, record, generation, "prepare")
                transition["phase"] = "disabling"
                self.jobs.save(session, record)
                if owner["host"] != "local":
                    previous = await self.call(owner["host"], record, generation, "disable")
                    if previous.get("result", {}).get("terminal"):
                        record.update(previous["result"])
                if target != "local" and not record.get("terminal"):
                    await self.call(target, record, generation, "enable")
                record["monitor"] = owner = {"host": target, "generation": generation}
                self.jobs.save(session, record)
            except AgentError as exc:
                owner["error"] = exc.message
                self.jobs.save(session, record)
                if not transition.get("phase"):
                    if owner["host"] == "local":
                        return None
                    old = await self.call(owner["host"], record, owner["generation"], "read")
                    return old.get("result") or {
                        "status": record.get("status", "pending"),
                        "terminal": False,
                    }
                # An uncertain handoff never starts a replacement observer or submits a job.
                raise
        if record.get("terminal"):
            return {k: v for k, v in record.items() if k != "monitor"}
        if owner["host"] == "local":
            return None
        observed = await self.call(owner["host"], record, owner["generation"], "read")
        if observed.get("error"):
            owner["error"] = observed["error"]
            self.jobs.save(session, record)
            raise AgentError("monitor_observation", observed["error"], 503)
        age = time.time() - observed.get("observed_at", 0)
        if age > 120 and not observed.get("result", {}).get("terminal"):
            # Start is idempotent under worker.lock; does not reset or resubmit any job.
            await self.call(owner["host"], record, owner["generation"], "enable")
        owner["observed_at"] = observed.get("observed_at")
        owner.pop("error", None)
        return observed.get("result") or {
            "status": record.get("status", "pending"),
            "terminal": False,
        }
