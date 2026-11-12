"""Software-independent job inputs and explicit artifact dependencies."""

from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from chemistry_agent.automation.models import Resources, StrictModel


def artifact_path(value: str, *, reserved: bool = True) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path == PurePosixPath(".")
        or any(p in {"..", "archive"} for p in path.parts)
        or "\x00" in value
        or "\\" in value
    ):
        raise ValueError("文件须使用工作目录内的相对路径，不能含 archive 或 ..。")
    if reserved and (
        path.parts[0].startswith(("_chem_", "_aiidasubmit"))
        or path.parts[0]
        in {
            "inputs.json",
            "file_inputs.json",
            "model.py",
            "sandbox.sb",
            "_job_inputs",
            "_dependency_cache",
            "attempts",
        }
    ):
        raise ValueError("此文件名保留给任务管理器。")
    return str(path)


class FileReference(StrictModel):
    node: str | None = None
    job_id: UUID | None = None
    file_id: UUID | None = None
    path: str = ""
    target: str
    source: Literal["retrieved", "remote"] = "retrieved"

    @model_validator(mode="after")
    def source_and_path(self):
        if sum(v is not None for v in (self.node, self.job_id, self.file_id)) != 1:
            raise ValueError("文件来源指定 node、job_id 或 file_id，三选一。")
        self.target = artifact_path(self.target)
        if not self.file_id:
            self.path = artifact_path(self.path, reserved=False)
            if any(c in self.path for c in "*?["):
                raise ValueError("依赖须指定实际文件路径，不使用通配符。")
        return self


class JobSettings(StrictModel):
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    workdir: str = Field(default="~/.chem-agent/jobs", min_length=1)
    adopted: bool = False
    script: str = Field(default="", max_length=200000)
    resources: Resources | None = None
    files: dict[str, str] = Field(
        default_factory=dict, description="Text input files, path: content"
    )
    retrieve: list[str] = Field(default_factory=list, description="Output paths/globs to retrieve")
    required_outputs: list[str] = Field(default_factory=list)
    scheduler_commands: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def paths(self):
        if not self.adopted and (not self.script.strip() or self.resources is None):
            raise ValueError("新提交的任务需要 script 和 resources；已有作业使用接管工具登记。")
        if "archive" in PurePosixPath(self.workdir).parts or "\x00" in self.workdir:
            raise ValueError("工作目录不能含 archive 或空字符。")
        self.files = {artifact_path(k): v for k, v in self.files.items()}
        self.retrieve = [artifact_path(k) for k in self.retrieve]
        self.required_outputs = [artifact_path(k) for k in self.required_outputs]
        if any(any(c in p for c in "*?[") for p in self.required_outputs):
            raise ValueError("required_outputs 使用确切路径。")
        # Resource allocations belong to the reviewed resource fields. Site-specific
        # Slurm directives (QOS, constraints, CPUs per task, licenses) remain available.
        lines = self.scheduler_commands.splitlines()
        if any(line.strip() and not line.lstrip().startswith("#SBATCH ") for line in lines):
            raise ValueError("scheduler_commands 仅填写 #SBATCH 指令；执行命令写入 script。")
        return self


class JobAdopt(StrictModel):
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    scheduler_job_id: str = Field(pattern=r"^[0-9]+(?:_[0-9]+)?$")
    remote_directory: str | None = None
    submitted_at: str | None = Field(default=None, pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
    plan_id: UUID | None = None
    title: str = Field(default="接管已有作业", min_length=1, max_length=120)
    instruction: str = Field(default="检查实际输出，分析结果并继续已授权的任务。", max_length=12000)
    retrieve: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)
