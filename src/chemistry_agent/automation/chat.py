"""Session-scoped calculation artifacts and tools. Submission is a separate user action."""

import asyncio
import json
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field, model_validator

from chemistry_agent.automation.modeling import execute_model
from chemistry_agent.automation.models import (
    StrictModel,
    StructureInput,
)
from chemistry_agent.automation.progress import ProgressStore
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.records import RecordRepository
from chemistry_agent.tools import Tool, ToolContext

if TYPE_CHECKING:
    from chemistry_agent.store import Store


class ImportInput(StrictModel):
    name: str = Field(default="structure", min_length=1, max_length=120)
    structure: StructureInput | None = None
    file_id: UUID | None = Field(default=None, description="Original file attached to this chat")
    format: Literal["vasp", "cif", "extxyz"] | None = None

    @model_validator(mode="after")
    def one_source(self):
        if (self.file_id is None) == (self.structure is None):
            raise ValueError("请提供附件 ID 或结构内容。")
        return self


class ModelInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=50000)
    input_structure_id: UUID | None = None
    output_format: Literal["extxyz", "vasp"] = "extxyz"


class ArtifactInput(StrictModel):
    kind: Literal["structures", "model_records", "previews", "submissions", "analyses"]
    artifact_id: UUID
    code_offset: int = Field(default=0, ge=0, le=50000)


class ArtifactListInput(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=30)


class CalculationChat:
    def __init__(self, service: AutomationService):
        from chemistry_agent.automation.plans import CalculationPlans

        self.service = service
        self.attachment_store: Store | None = None
        self.root = service.settings.platform_path / "chat"
        self.records = RecordRepository(self.root)
        self.model_lock = asyncio.Lock()
        self.import_lock = asyncio.Lock()
        self.plans = CalculationPlans(self)

    @cached_property
    def progress(self) -> ProgressStore:
        return ProgressStore(self.service.settings.platform_path / "task-monitor.sqlite3")

    def directory(self, session_id: str, kind: str) -> Path:
        return self.records.directory(session_id, kind)

    def save(self, session_id: str, kind: str, identifier: str, data: dict) -> None:
        self.records.save(session_id, kind, identifier, data)

    def load(self, session_id: str, kind: str, identifier: str) -> dict:
        return self.records.load(session_id, kind, identifier)

    def list_records(
        self, session_id: str, kind: str, offset: int = 0, limit: int = 100
    ) -> list[dict]:
        return self.records.list_records(session_id, kind, offset, limit)

    async def import_structure(self, args: ImportInput, context: ToolContext) -> dict:
        if args.file_id:
            from chemistry_agent.attachments import session_file

            if self.attachment_store is None:
                raise AgentError("file_not_found", "此对话中没有该附件。", 404)
            name, source = await session_file(
                self.attachment_store, context.session_id, args.file_id
            )
            fmt = args.format or (
                "cif"
                if name.lower().endswith(".cif")
                else "extxyz"
                if name.lower().endswith((".xyz", ".extxyz"))
                else "vasp"
            )
            identifier = str(uuid5(UUID(context.session_id), f"file:{args.file_id}:{fmt}"))
            async with self.import_lock:
                target = self.directory(context.session_id, "structures") / f"{identifier}.json"
                if target.is_file():
                    return self.structure_summary(
                        self.load(context.session_id, "structures", identifier)
                    )
                if isinstance(source, Path):
                    validated = await self.service.call(
                        "validate_structure_file", {"file": str(source), "format": fmt}
                    )
                else:
                    try:
                        original = source.decode("utf-8-sig")
                    except UnicodeDecodeError as exc:
                        raise AgentError(
                            "invalid_structure", "结构输入需要 UTF-8 文本。", 422
                        ) from exc
                    validated = await self.service.call(
                        "validate_structure_file",
                        {"text": original, "format": fmt},
                    )
                data = {
                    "id": identifier,
                    "name": name,
                    "source_file_id": str(args.file_id),
                    **validated,
                }
                self.save(context.session_id, "structures", identifier, data)
                return self.structure_summary(data)
        validated = await self.service.call("validate_structure", args.structure.model_dump())
        identifier = str(uuid4())
        data = {"id": identifier, "name": args.name, **validated}
        self.save(context.session_id, "structures", identifier, data)
        return self.structure_summary(data)

    @staticmethod
    def structure_summary(data: dict) -> dict:
        return {key: value for key, value in data.items() if key not in {"structure", "extxyz"}}

    async def build_structure(self, args: ModelInput, context: ToolContext) -> dict:
        identifier = str(uuid4())
        directory = self.directory(context.session_id, "models") / identifier
        directory.mkdir(mode=0o700)
        if args.input_structure_id:
            original = self.load(context.session_id, "structures", str(args.input_structure_id))
            (directory / "input.extxyz").write_text(original["extxyz"])
            # POSCAR preserves fractional-direction constraints for ASE modeling.
            import io

            from ase.io import read

            from chemistry_agent.automation.constraints import poscar_text, restore_constraints

            atoms = read(io.StringIO(original["extxyz"]), format="extxyz")
            restore_constraints(atoms)
            (directory / "input.vasp").write_text(poscar_text(atoms))
        record = {
            "id": identifier,
            "name": args.name,
            "code": args.code,
            "status": "running",
            "input_structure_id": str(args.input_structure_id or ""),
        }
        self.save(context.session_id, "model_records", identifier, record)
        try:
            async with self.model_lock:
                content = await execute_model(
                    directory, args.code, output_format=args.output_format
                )
            result = await self.import_structure(
                ImportInput(
                    name=args.name,
                    structure=StructureInput(format=args.output_format, content=content),
                ),
                context,
            )
        except AgentError as exc:
            record.update(status="failed", error=exc.message)
            self.save(context.session_id, "model_records", identifier, record)
            raise
        record.update(status="completed", structure_id=result["id"])
        self.save(context.session_id, "model_records", identifier, record)
        return {**result, "model_id": identifier}

    async def artifacts(self, args: ArtifactListInput, context: ToolContext) -> dict:
        return {
            "plans": [
                {key: item[key] for key in ("id", "title", "revision", "status")}
                for item in self.plans.records(context.session_id)
            ],
            "analyses": [
                {key: item.get(key) for key in ("id", "name", "status")}
                for item in self.list_records(context.session_id, "analyses")
            ],
            "structures": [
                self.structure_summary(item)
                for item in self.list_records(
                    context.session_id, "structures", args.offset, args.limit
                )
            ],
            "previews": [
                {key: item[key] for key in ("preview_id", "structure", "resources", "structure_id")}
                for item in self.list_records(
                    context.session_id, "previews", args.offset, args.limit
                )
            ],
            "models": [
                {key: value for key, value in item.items() if key != "code"}
                for item in self.list_records(
                    context.session_id, "model_records", args.offset, args.limit
                )
            ],
            "submissions": [
                {"preview_id": path.stem, **json.loads(path.read_text())}
                for path in sorted(
                    self.directory(context.session_id, "submissions").glob("*.json"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )[args.offset : args.offset + args.limit]
            ],
            "offset": args.offset,
            "next_offset": args.offset + args.limit,
            "total": {
                kind: len(list(self.directory(context.session_id, kind).glob("*.json")))
                for kind in ("structures", "previews", "model_records", "submissions")
            },
        }

    async def artifact(self, args: ArtifactInput, context: ToolContext) -> dict:
        data = self.load(context.session_id, args.kind, str(args.artifact_id))
        if args.kind == "structures":
            return self.structure_summary(data)
        if args.kind == "model_records":
            code = data["code"]
            return {
                **data,
                "code": code[args.code_offset : args.code_offset + 6000],
                "code_offset": args.code_offset,
                "code_characters": len(code),
                "next_code_offset": args.code_offset + 6000
                if len(code) > args.code_offset + 6000
                else None,
            }
        if args.kind == "previews":
            # Stored request is exact; exclude raw coordinates and generated file bodies.
            return {
                **{key: value for key, value in data.items() if key not in {"files", "request"}},
                "request": {
                    key: value for key, value in data["request"].items() if key != "structure"
                },
            }
        return data

    async def require_session_job(self, session_id: str, identifier: str) -> None:
        """Verify historical job ownership before serving saved metadata."""
        records = self.list_records(session_id, "submissions", limit=100000)
        if any(identifier in {item["job"]["id"], str(item["job"]["pk"])} for item in records):
            return
        if not records:
            raise AgentError("job_not_in_session", "当前对话尚无已提交的计算流程。", 403)
        await self.service.call(
            "session_job",
            {"job_id": identifier, "root_ids": [item["job"]["id"] for item in records]},
        )

    async def output_structure(self, session: str, identifier: str) -> dict:
        """Read the actual final output, including for jobs never manually imported."""
        await self.require_session_job(session, identifier)
        return await self.service.call("job_structure", {"job_id": identifier})

    def tools(self) -> list[Tool]:
        return self.plans.tools() + [
            Tool(
                "import_structure",
                "Validate and save a periodic geometry from this chat.",
                ImportInput,
                self.import_structure,
                True,
            ),
            Tool(
                "build_structure",
                "Run offline ASE modeling; preserve constraints and provenance.",
                ModelInput,
                self.build_structure,
                True,
            ),
            Tool(
                "calculation_artifacts",
                "List this chat's saved structures and historical records.",
                ArtifactListInput,
                self.artifacts,
                True,
            ),
            Tool(
                "calculation_artifact",
                "Read one exact saved artifact from this chat.",
                ArtifactInput,
                self.artifact,
                True,
            ),
        ]
