"""Immutable file snapshots with source identity, usable without a chat model."""

import asyncio
import base64
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from chemistry_agent.automation.job_models import artifact_path
from chemistry_agent.automation.models import StrictModel
from chemistry_agent.platform.contracts import OperationContext
from chemistry_agent.store import utc_now


class ArtifactRegister(StrictModel):
    job_id: UUID
    path: str
    key: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def valid_path(self):
        self.path = artifact_path(self.path, reserved=False)
        return self


class ArtifactRead(StrictModel):
    artifact_id: UUID | None = None
    content: bool = False
    encoding: Literal["text", "base64"] = "text"
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=16000, ge=1, le=64000)


class ArtifactOperations:
    def __init__(self, plans) -> None:
        self.plans = plans
        self.locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def describe(artifact: dict) -> dict:
        """Distinguish immutable storage from a capture of a still-running source."""
        return artifact | {
            "immutable": True,
            "source_state_at_capture": "running" if artifact["snapshot"] else "terminal",
            "snapshot_meaning": "snapshot=true means captured before the job ended; "
            "both true and false are immutable stored copies",
        }

    async def register(self, args: ArtifactRegister, context: OperationContext) -> dict:
        session = context.session_id
        identifier = str(
            uuid5(NAMESPACE_URL, f"artifact:{session}:{args.job_id}:{args.path}:{args.key}")
        )
        records = self.plans.chat.records
        async with self.locks.setdefault(identifier, asyncio.Lock()):
            if (records.directory(session, "artifacts") / f"{identifier}.json").exists():
                return self.describe(records.load(session, "artifacts", identifier))
            job = self.plans.slurm.load(session, str(args.job_id))
            target = records.directory(session, "artifact_blobs") / identifier
            manifest = await self.plans.slurm.download(job, args.path, target)
            artifact = {
                "id": identifier,
                "job_id": str(args.job_id),
                "source_path": args.path,
                "source_identity": job.get("identity"),
                "observed_at": utc_now(),
                "snapshot": not job.get("terminal", False),
                "key": args.key,
                "size": manifest["size"],
                "sha256": manifest["sha256"],
            }
            records.save(session, "artifacts", identifier, artifact)
            return self.describe(artifact)

    async def read(self, args: ArtifactRead, context: OperationContext) -> dict:
        records, session = self.plans.chat.records, context.session_id
        if not args.artifact_id:
            return {
                "artifacts": [
                    self.describe(item) for item in records.list_records(session, "artifacts")
                ]
            }
        artifact = self.describe(records.load(session, "artifacts", str(args.artifact_id)))
        if not args.content:
            return artifact
        path = records.directory(session, "artifact_blobs") / str(args.artifact_id)
        with path.open("rb") as stream:
            stream.seek(args.offset)
            data = stream.read(args.length)
        return {
            **artifact,
            "encoding": args.encoding,
            "content": data.decode(errors="replace")
            if args.encoding == "text"
            else base64.b64encode(data).decode(),
            "next_offset": args.offset + len(data)
            if args.offset + len(data) < artifact["size"]
            else None,
        }
