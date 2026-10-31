"""Session-scoped calculation artifacts and tools. Submission is a separate user action."""

import asyncio
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from chemistry_agent.automation.modeling import execute_model
from chemistry_agent.automation.models import PreviewRequest, StrictModel, StructureInput
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext


class EmptyInput(StrictModel):
    pass


class ImportInput(StrictModel):
    name: str = Field(default="structure", min_length=1, max_length=120)
    structure: StructureInput


class ModelInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=50000)
    input_structure_id: UUID | None = None


class ChatPreviewInput(PreviewRequest):
    kind: Literal["vasp"] = "vasp"
    structure: StructureInput | None = None
    structure_id: UUID


class JobInput(StrictModel):
    job_id: str = Field(min_length=1, max_length=80, description="AiiDA UUID or numeric PK")


class PrepareInput(StrictModel):
    workflow: Literal["single", "relax_static"] = "single"
    structure_id: UUID
    protocol_id: str | None = Field(default=None, max_length=64)
    incar: dict = Field(default_factory=dict, max_length=200)
    kmesh: tuple[int, int, int] | None = None


class ArtifactInput(StrictModel):
    kind: Literal["structures", "model_records", "previews", "submissions"]
    artifact_id: UUID
    code_offset: int = Field(default=0, ge=0, le=50000)


class ArtifactListInput(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=30)


class CalculationChat:
    def __init__(self, service: AutomationService):
        self.service = service
        self.root = service.settings.aiida_path / "chat"
        self.model_lock = asyncio.Lock()

    def directory(self, session_id: str, kind: str) -> Path:
        directory = self.root / str(UUID(session_id)) / kind
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def save(self, session_id: str, kind: str, identifier: str, data: dict) -> None:
        path = self.directory(session_id, kind) / f"{UUID(identifier)}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def load(self, session_id: str, kind: str, identifier: str) -> dict:
        path = self.directory(session_id, kind) / f"{UUID(identifier)}.json"
        if not path.is_file():
            raise AgentError("artifact_not_found", "此对话中没有该结构或预览，请重新选择。", 404)
        return json.loads(path.read_text())

    def list_records(
        self, session_id: str, kind: str, offset: int = 0, limit: int = 100
    ) -> list[dict]:
        paths = sorted(
            self.directory(session_id, kind).glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [json.loads(path.read_text()) for path in paths[offset : offset + limit]]

    async def import_structure(self, args: ImportInput, context: ToolContext) -> dict:
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
                content = await execute_model(directory, args.code)
            result = await self.import_structure(
                ImportInput(
                    name=args.name, structure=StructureInput(format="extxyz", content=content)
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

    async def setup(self, args: EmptyInput, context: ToolContext) -> dict:
        result = await self.service.call("catalog")
        for code in result["codes"]:
            code.pop("environment", None)
        return result

    async def artifacts(self, args: ArtifactListInput, context: ToolContext) -> dict:
        return {
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

    async def prepare(self, args: PrepareInput, context: ToolContext) -> dict:
        structure = self.load(context.session_id, "structures", str(args.structure_id))
        resolved = await self.service.call(
            "resolve_calculation",
            {
                **args.model_dump(mode="json", exclude={"structure_id"}),
                "structure": structure["structure"],
            },
        )
        request = {key: value for key, value in resolved["request"].items() if key != "structure"}
        request["workflow"] = args.workflow
        result = await self.preview(
            ChatPreviewInput(structure_id=args.structure_id, **request), context
        )
        saved = self.load(context.session_id, "previews", result["preview_id"])
        provenance = {
            key: resolved[key] for key in ("protocol", "potentials", "scientific_validation")
        }
        saved.update(provenance)
        self.save(context.session_id, "previews", result["preview_id"], saved)
        return {**result, **provenance}

    async def preview(self, args: ChatPreviewInput, context: ToolContext) -> dict:
        if args.structure is not None:
            raise AgentError("use_structure_id", "请用结构 ID 引用已上传或建模的结构。", 422)
        structure = self.load(context.session_id, "structures", str(args.structure_id))
        payload = args.model_dump(mode="json", exclude={"structure_id"})
        payload["structure"] = structure["structure"]
        result = await self.service.call("preview", payload)
        result.update(
            structure_id=structure["id"],
            request=payload,
            confirmation_required=True,
            created_in_run=context.run_id,
        )
        self.save(context.session_id, "previews", result["preview_id"], result)
        return {key: value for key, value in result.items() if key not in {"request", "files"}} | {
            "incar": payload["incar"],
            "kmesh": payload["kmesh"],
            "potential_family": payload["potential_family"],
            "potential_mapping": payload["potential_mapping"],
            "input_files": list(result["files"]),
            "review": "完整输入与脚本已保存在聊天预览卡片中，等待用户确认。",
        }

    async def jobs(self, args: EmptyInput, context: ToolContext) -> dict:
        return await self.service.call("jobs")

    async def job(self, args: JobInput, context: ToolContext) -> dict:
        result = await self.service.call("job_details", args.model_dump())
        monitor = getattr(self, "monitor", None)
        if monitor:
            result["recovery"] = monitor.events.recovery(result["id"])
        return result

    async def events(self, args: EmptyInput, context: ToolContext) -> dict:
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return {"enabled": False, "events": []}
        inbox = monitor.events.inbox(context.session_id, limit=10)
        # Do not duplicate potentially large raw results in the model context.
        inbox["events"] = [
            {key: event[key] for key in ("id", "job_id", "kind", "title", "created_at", "read_at")}
            for event in inbox["events"]
        ]
        return inbox

    async def confirm(self, session_id: str, preview_id: str) -> dict:
        self.load(session_id, "previews", preview_id)
        # AiiDA handles stale previews and persistent idempotency, including uncertain submissions.
        result = await self.service.call("submit", {"preview_id": preview_id})
        self.save(session_id, "submissions", preview_id, result)
        return result

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "calculation_events",
                "Read this session's task notifications and background monitor freshness. "
                "Use calculation_job for current results and Agent recovery audit.",
                EmptyInput,
                self.events,
                True,
            ),
            Tool(
                "calculation_setup",
                "List configured clusters, VASP executables and potential "
                "family mappings. Use real IDs before preparing a calculation.",
                EmptyInput,
                self.setup,
                True,
            ),
            Tool(
                "import_structure",
                "Validate and save one periodic structure supplied in chat.",
                ImportInput,
                self.import_structure,
                True,
            ),
            Tool(
                "build_structure",
                "Run ASE Python modeling code in an isolated, offline process. "
                "Write structure.extxyz. Optional input is input.extxyz. No SSH or submission.",
                ModelInput,
                self.build_structure,
                True,
            ),
            Tool(
                "calculation_artifacts",
                "Recover this conversation's structure, model, preview and submitted job IDs. "
                "Submission records are historical; query calculation_job for current state.",
                ArtifactListInput,
                self.artifacts,
                True,
            ),
            Tool(
                "calculation_artifact",
                "Read an exact saved model code, preview parameters or submission record "
                "after context compression. Only this session's artifacts are accessible.",
                ArtifactInput,
                self.artifact,
                True,
            ),
            Tool(
                "calculation_prepare",
                "Prepare a VASP preview from structure_id and a saved protocol. Resolves "
                "server/code/resources and imports missing POTCARs from user-approved sources. "
                "Optional INCAR/kmesh overrides are shown in preview. Never submits. "
                "Check protocol scope using calculation_setup first.",
                PrepareInput,
                self.prepare,
                True,
            ),
            Tool(
                "calculation_preview",
                "Generate actual VASP inputs and scheduler script from a "
                "saved structure_id. Does NOT submit. User must click the preview confirmation.",
                ChatPreviewInput,
                self.preview,
                True,
            ),
            Tool(
                "calculation_jobs",
                "Read recent managed AiiDA job states and results.",
                EmptyInput,
                self.jobs,
                True,
            ),
            Tool(
                "calculation_job",
                "Read one managed AiiDA job, retrieved file list and bounded "
                "diagnostic logs. Use AiiDA UUID or PK, not a Slurm job number.",
                JobInput,
                self.job,
                True,
            ),
        ]
