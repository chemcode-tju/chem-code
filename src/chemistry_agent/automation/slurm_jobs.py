"""Durable Slurm jobs without an AiiDA profile, daemon or software-specific plugin."""

import asyncio
import base64
import hashlib
import json
import shlex
import sqlite3
from pathlib import Path
from uuid import UUID

from chemistry_agent.automation import queue_remote, result_reader, slurm_remote
from chemistry_agent.automation.job_models import JobSettings, artifact_path
from chemistry_agent.automation.ssh_profiles import load_profile, shell_profiles, write_private
from chemistry_agent.errors import AgentError
from chemistry_agent.store import utc_now

CHUNK_BYTES = 1024 * 1024
REMOTE_HELPER = (
    Path(slurm_remote.__file__)
    .read_text()
    .replace(
        "from chemistry_agent.automation.result_reader import result_snapshot",
        Path(result_reader.__file__).read_text(),
    )
)
QUEUE_HELPER = Path(queue_remote.__file__).read_text()


def input_files(directory: Path) -> list[Path]:
    pending, files = [directory], []
    while pending:
        for path in pending.pop().iterdir():
            if path.name == "archive" or path.is_symlink():
                continue
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files.append(path)
    return sorted(files)


def submission_script(identifier: str, settings: JobSettings) -> str:
    r = settings.resources
    lines = [
        "#!/bin/bash",
        settings.scheduler_commands,
        f"#SBATCH --job-name=chem-{identifier}",
        f"#SBATCH --partition={r.queue}",
        f"#SBATCH --nodes={r.nodes}",
        f"#SBATCH --ntasks-per-node={r.ranks_per_node}",
        f"#SBATCH --time={r.wallclock_seconds // 60}:{r.wallclock_seconds % 60:02d}",
        f"#SBATCH --mem={r.memory_mb}",
        "#SBATCH --output=_chem_stdout.txt",
        "#SBATCH --error=_chem_stderr.txt",
    ]
    if r.account:
        lines.append(f"#SBATCH --account={r.account}")
    if r.gpus_per_node:
        gpu = f"{r.gpu_type}:" if r.gpu_type else ""
        lines.append(f"#SBATCH --gres=gpu:{gpu}{r.gpus_per_node}")
    lines.extend(
        [
            'suffix="${SLURM_ARRAY_TASK_ID:+_$SLURM_ARRAY_TASK_ID}"',
            'exec > "_chem_stdout${suffix}.txt" 2> "_chem_stderr${suffix}.txt"',
            "bash -e -o pipefail _chem_user.sh",
            "result=$?",
            'printf \'%s\\n\' "$result" > "_chem_exit_code${suffix}.tmp"',
            'mv "_chem_exit_code${suffix}.tmp" "_chem_exit_code${suffix}"',
            'exit "$result"',
        ]
    )
    return "\n".join(lines) + "\n"


class SlurmJobs:
    def __init__(self, chat) -> None:
        self.chat = chat
        self.settings = chat.service.settings
        self.root = self.settings.platform_path
        self.locks: dict[str, asyncio.Lock] = {}
        from chemistry_agent.hosting.monitor import MonitorPlacement

        self.placement = MonitorPlacement(self)

    def hosts(self) -> list[str]:
        from chemistry_agent.environments.registry import Environments

        return Environments(self.root).available_hosts(self.settings.allowed_ssh_hosts)

    def load(self, session: str, identifier: str) -> dict:
        return self.chat.load(session, "slurm_jobs", identifier)

    def save(self, session: str, record: dict) -> None:
        write_private(self.chat.directory(session, "slurm_jobs") / f"{record['id']}.json", record)

    async def request(self, host: str, payload: dict) -> dict:
        from chemistry_agent.environments.registry import Environments

        environments = Environments(self.root)
        if host not in self.hosts():
            raise AgentError("job_host_unknown", "请先登记该 SSH 服务器别名。", 422)
        environment = environments.get(host)
        if environment:
            if environment.transport != "ssh":
                return await environments.slurm(host, payload)
            host = environment.ssh_alias or host
        source = QUEUE_HELPER if payload["action"] in queue_remote.QUEUE_ACTIONS else REMOTE_HELPER
        command = "python3 -c " + shlex.quote(source)
        encoded = json.dumps(payload).encode()
        process = None
        try:
            async with asyncio.timeout(1800 if payload["action"] == "copy" else 45):
                profiles = shell_profiles(self.root)
                if host in profiles:
                    from chemistry_agent.automation.ssh_connection import connect

                    async with await connect(
                        self.root, load_profile(self.root, profiles[host])
                    ) as ssh:
                        response = await ssh.run(command, input=encoded, encoding=None, check=False)
                        stdout, stderr, code = (
                            response.stdout,
                            response.stderr,
                            response.exit_status,
                        )
                else:
                    process = await asyncio.create_subprocess_exec(
                        "ssh",
                        "-T",
                        "-oBatchMode=yes",
                        "-oStrictHostKeyChecking=yes",
                        "-oConnectTimeout=8",
                        "-oForwardAgent=no",
                        "-oForwardX11=no",
                        "-oClearAllForwardings=yes",
                        "-oPermitLocalCommand=no",
                        "--",
                        host,
                        command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await process.communicate(encoded)
                    code = process.returncode
                if code:
                    raise AgentError("job_ssh_failed", stderr.decode(errors="replace")[-4000:], 503)
                result = json.loads(stdout)
                if result.get("error"):
                    raise AgentError(
                        "job_remote_error",
                        result["error"],
                        422
                        if result["error"].startswith(("FileNotFoundError:", "ValueError:"))
                        else 502,
                    )
                return result
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError("job_transport_unavailable", f"SSH 操作未完成：{exc}", 503) from exc
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()

    async def remote(self, record: dict, action: str, **payload) -> dict:
        if record.get("origin") == "adopted" and action in {"status", "cancel"}:
            action = "observe_external" if action == "status" else "cancel_external"
            payload.update(scheduler_job_id=record["scheduler_job_id"], identity=record["identity"])
        return await self.request(
            record["host"], {"root": record["remote_directory"], "action": action, **payload}
        )

    async def upload(self, record: dict, directory: Path) -> None:
        for path in await asyncio.to_thread(input_files, directory):
            relative = path.relative_to(directory)
            if "archive" in relative.parts or path.is_symlink() or not path.is_file():
                continue
            offset = 0
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(CHUNK_BYTES)
                    if not chunk and offset:
                        break
                    result = await self.remote(
                        record,
                        "upload",
                        path=str(relative),
                        offset=offset,
                        data=base64.b64encode(chunk).decode(),
                    )
                    if result.get("sealed"):
                        return
                    if not chunk:
                        break
                    offset += len(chunk)

    async def submit(
        self, session: str, identifier: str, settings: JobSettings, directory: Path
    ) -> dict:
        """Persist one immutable local receipt before any possible sbatch side effect."""
        if settings.adopted:
            raise AgentError(
                "adopted_not_submittable", "接管记录不能提交；请明确提供新尝试的脚本和资源。", 409
            )
        async with self.locks.setdefault(identifier, asyncio.Lock()):
            path = self.chat.directory(session, "slurm_jobs") / f"{UUID(identifier)}.json"
            if path.exists():
                record = self.load(session, identifier)
                if record["settings"] != settings.model_dump(mode="json"):
                    raise AgentError("job_input_changed", "原任务输入已锁定，请创建新尝试。", 409)
            else:
                if settings.host not in self.hosts():
                    raise AgentError("job_host_unknown", "请先登记 SSH 服务器。", 422)
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                (directory / "_chem_user.sh").write_text(settings.script)
                (directory / "_chem_submit.sh").write_text(submission_script(identifier, settings))
                digest = hashlib.sha256(settings.model_dump_json().encode())
                for file in await asyncio.to_thread(input_files, directory):
                    if file.is_file() and not file.is_symlink() and "archive" not in file.parts:
                        digest.update(str(file.relative_to(directory)).encode())
                        with file.open("rb") as stream:
                            digest.update(hashlib.file_digest(stream, "sha256").digest())
                record = {
                    "id": identifier,
                    "session_id": session,
                    "host": settings.host,
                    "settings": settings.model_dump(mode="json"),
                    "digest": digest.hexdigest(),
                    "remote_directory": settings.workdir.rstrip("/") + "/" + identifier,
                    "status": "preparing",
                    "created_at": utc_now(),
                }
                self.save(session, record)
            if record.get("scheduler_job_id") or record["status"] in {"quota_wait", "rejected"}:
                return record
            await self.upload(record, directory)
            record["status"] = "uncertain"
            self.save(session, record)
            result = await self.remote(record, "submit", id=identifier, digest=record["digest"])
            record.update(result, checked_at=utc_now())
            if record["status"] == "quota_wait":
                record.setdefault("quota_wait_since", utc_now())
            self.save(session, record)
            return record

    def release_slot(self, record: dict) -> str | None:
        """Consume one terminal-job event in the same host/account/partition, never a timer."""
        database_path = self.root / "slurm-completion-releases.sqlite3"
        with sqlite3.connect(database_path) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS releases (event TEXT PRIMARY KEY, waiter TEXT NOT NULL)"
            )
            saved = database.execute(
                "SELECT event FROM releases WHERE waiter=?", (record["id"],)
            ).fetchall()
            # Recover a release persisted just before a process restart.
            for row in saved:
                if row[0] not in record.get("releases", []):
                    return row[0]
            for path in sorted(self.chat.root.glob("*/slurm_jobs/*.json")):
                other = json.loads(path.read_text())
                if (
                    other.get("terminal")
                    and other.get("scheduler_job_id")
                    and other.get("host") == record["host"]
                    and other.get("user") == record.get("user")
                    and (
                        other.get("queue")
                        or (other["settings"].get("resources") or {}).get("queue")
                    )
                    == record["settings"]["resources"]["queue"]
                    and other.get("completed_at", "") > record.get("quota_wait_since", "~")
                ):
                    event_key = other.get("completion_key", other["id"])
                    cursor = database.execute(
                        "INSERT OR IGNORE INTO releases VALUES (?,?)", (event_key, record["id"])
                    )
                    if cursor.rowcount:
                        return event_key
        return None

    async def inspect(self, session: str, identifier: str, *, release_waiter: bool = False) -> dict:
        async with self.locks.setdefault(identifier, asyncio.Lock()):
            record = self.load(session, identifier)
            if record.get("terminal"):
                return record
            result = await self.placement.inspect(session, record)
            if result is None:
                result = await self.remote(record, "status")
            record.update(result, checked_at=utc_now())
            if record["status"] == "quota_wait":
                record.setdefault("quota_wait_since", utc_now())
                self.save(session, record)
                release = self.release_slot(record) if release_waiter else None
                if release:
                    record.update(
                        await self.remote(
                            record,
                            "submit",
                            id=identifier,
                            digest=record["digest"],
                            release=release,
                        )
                    )
            if record.get("terminal"):
                record.setdefault("completed_at", utc_now())
            self.save(session, record)
            return record

    async def download(self, record: dict, name: str, destination: Path) -> dict:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        offset, digest = 0, hashlib.sha256()
        with temporary.open("wb") as stream:
            while True:
                result = await self.remote(
                    record, "read", path=name, offset=offset, length=CHUNK_BYTES
                )
                data = base64.b64decode(result["data"], validate=True)
                stream.write(data)
                digest.update(data)
                offset += len(data)
                if offset >= result["size"]:
                    break
                if not data:
                    raise AgentError("job_file_incomplete", "读取文件未完成。", 502)
        temporary.replace(destination)
        return {"path": name, "size": offset, "sha256": digest.hexdigest()}

    async def retrieve(self, record: dict, directory: Path) -> dict:
        settings = JobSettings.model_validate(record["settings"])
        patterns = list(
            dict.fromkeys(
                [
                    *settings.retrieve,
                    *settings.required_outputs,
                    "_chem_stdout*.txt",
                    "_chem_stderr*.txt",
                    "_chem_exit_code*",
                ]
            )
        )
        listing = await self.remote(record, "files", patterns=patterns)
        manifest = {}
        for entry in listing["files"]:
            name = entry["path"]
            # Internal diagnostics are downloadable; all other names use artifact validation.
            if not name.startswith("_chem_"):
                artifact_path(name)
            manifest[name] = await self.download(record, name, directory / name)
        record["retrieved_directory"] = str(directory)
        record["retrieved_manifest"] = manifest
        self.save(record["session_id"], record)
        return {
            "files": list(manifest),
            "manifest": manifest,
            "missing_outputs": sorted(set(settings.required_outputs) - manifest.keys()),
        }
