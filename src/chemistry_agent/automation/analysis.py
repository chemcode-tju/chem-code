"""Agent-facing file exploration and code-based analysis of owned calculation jobs."""

import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.tools import Tool, ToolContext


class JobFilesInput(StrictModel):
    job_id: str
    mode: Literal["list", "read"] = "list"
    source: Literal["retrieved", "remote"] = "retrieved"
    path: str = "."
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=64000)


class AnalyzeInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    jobs: dict[str, str] = Field(min_length=1)
    files: list[str] = Field(default_factory=lambda: ["*"])
    source: Literal["retrieved", "remote"] = "retrieved"
    code: str = Field(min_length=1, max_length=50000)

    @field_validator("jobs")
    @classmethod
    def aliases(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", alias) for alias in value):
            raise ValueError("任务别名应为小写字母开头的字母、数字、下划线。")
        return value


class CalculationAnalysis:
    def __init__(self, chat) -> None:
        self.chat = chat

    async def files(self, args: JobFilesInput, context: ToolContext) -> dict:
        await self.chat.require_session_job(context.session_id, args.job_id)
        return await self.chat.service.call("task_files", args.model_dump())

    async def analyze(self, args: AnalyzeInput, context: ToolContext) -> dict:
        identifier = str(uuid4())
        runtime, inputs = {}, {}
        for alias, job_id in args.jobs.items():
            await self.chat.require_session_job(context.session_id, job_id)
            job = await self.chat.service.call("job_details", {"job_id": job_id})
            runtime[alias] = {
                "status": "completed",
                "outputs": {
                    "result": {
                        **job.get("results", {}),
                        "job_id": job["id"],
                        "pk": job["pk"],
                    }
                },
            }
            inputs[alias] = {"node": alias, "output": "result", "structure_id": None}
        record = {
            "id": identifier,
            "name": args.name,
            "code": args.code,
            "jobs": args.jobs,
            "status": "running",
        }
        self.chat.save(context.session_id, "analyses", identifier, record)
        try:
            outputs = await self.chat.plans.run_code(
                context.session_id,
                {"id": identifier, "runtime": runtime},
                {
                    "id": "analysis",
                    "title": args.name,
                    "kind": "analysis",
                    "code": args.code,
                    "inputs": inputs,
                    "result_files": args.files,
                    "file_source": args.source,
                },
                {},
            )
        except Exception as exc:
            record.update(status="failed", error=str(exc))
            self.chat.save(context.session_id, "analyses", identifier, record)
            raise
        record.update(status="completed", result=outputs["result"])
        self.chat.save(context.session_id, "analyses", identifier, record)
        encoded = json.dumps(outputs["result"], ensure_ascii=False)
        result = (
            {"result": outputs["result"]}
            if len(encoded) <= 16000
            else {
                "result_excerpt": encoded[:16000],
                "full_result_saved": True,
            }
        )
        return {"analysis_id": identifier, "name": args.name, "status": "completed", **result}

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "calculation_files",
                "List any job directory or read file text in byte ranges. "
                "No extension whitelist. source=remote also accesses files not retrieved yet. "
                "For binary/large data use calculation_analyze, not large model context.",
                JobFilesInput,
                self.files,
                True,
            ),
            Tool(
                "calculation_analyze",
                "Run offline Python analysis on calculation output files. "
                "jobs maps aliases to owned AiiDA IDs. files are relative paths or ['*'] for all. "
                "Code reads inputs.json: alias.result is parsed data, alias.files maps file names "
                "to local paths (arbitrary formats, binary supported). Write result.json. "
                "Use ASE/numpy or custom parsers; no cluster jobs are submitted. "
                "Full code/result retained. Returns summary, not file contents.",
                AnalyzeInput,
                self.analyze,
                True,
            ),
        ]
