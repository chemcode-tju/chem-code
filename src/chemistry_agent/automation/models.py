import ipaddress
import re
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ServerCreate(StrictModel):
    label: Name
    hostname: str = Field(min_length=1, max_length=253)
    auth_mode: Literal["alias", "key", "password"] = "alias"
    username: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}$")
    port: int | None = Field(default=None, ge=1, le=65535)
    identity_file: str | None = Field(default=None, max_length=500)

    @field_validator("hostname")
    @classmethod
    def validate_host(cls, host: str) -> str:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host):
                raise ValueError("请填主机名、IP 或 SSH 别名；用户名和端口请分别填写。") from None
        return host

    @model_validator(mode="after")
    def validate_auth(self):
        if self.auth_mode == "alias":
            if self.username or self.port or self.identity_file:
                raise ValueError("别名模式使用本机 OpenSSH 配置中的用户名、端口和身份。")
        elif not self.username:
            raise ValueError("直接连接须填写 SSH 用户名。")
        if self.identity_file:
            if self.auth_mode != "key" or self.identity_file.endswith(".pub"):
                raise ValueError("密钥模式需要本地私钥路径。公钥安装在服务器，不能用于签名。")
            path = Path(self.identity_file).expanduser()
            if not path.is_absolute() or not path.resolve().is_relative_to(
                (Path.home() / ".ssh").resolve()
            ):
                raise ValueError("仅支持本机 ~/.ssh 内的私钥路径。")
        return self

    scheduler: Literal["slurm", "pbspro", "torque"] = "slurm"
    workdir: str = Field(min_length=2, max_length=240)

    @field_validator("workdir")
    @classmethod
    def validate_workdir(cls, value: str) -> str:
        if not value.startswith("/") or any(char in value for char in "\n\r\x00{}"):
            raise ValueError("工作目录须为绝对路径，不能含换行或模板占位符。")
        if value.rstrip("/") == "" or ".." in Path(value).parts or "archive" in Path(value).parts:
            raise ValueError("工作目录应为专用目录，不能是根目录、上级目录或 archive。")
        return value.rstrip("/")


class StructureInput(StrictModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    format: Literal["vasp", "cif", "extxyz"] = "vasp"


class Resources(StrictModel):
    queue: Name
    nodes: int = Field(default=1, ge=1, le=128)
    ranks_per_node: int = Field(default=1, ge=1, le=256)
    wallclock_seconds: int = Field(default=60, ge=30, le=604800)
    memory_mb: int = Field(default=512, ge=128, le=1048576)
    account: Name | None = None
    gpus_per_node: int = Field(default=0, ge=0, le=16)
    gpu_type: Name | None = None


class BranchSettings(StrictModel):
    """Optional step-specific numerical settings, without changing the energy reference."""

    nelm: int | None = Field(default=None, ge=1, le=400)
    nsw: int | None = Field(default=None, ge=1, le=1000)
    ediffg: float | None = Field(default=None, ge=-1, lt=0)


class SurfacePlan(StrictModel):
    surface: Literal["fcc100"] = "fcc100"
    size: tuple[int, int] = (2, 2)
    layers: int = Field(default=4, ge=2, le=30)
    fixed_layers: int = Field(default=2, ge=0, le=29)
    vacuum_angstrom: float = Field(default=20, ge=5, le=100)
    co_sites: list[Literal["top", "bridge", "hollow"]] = Field(default_factory=list, max_length=3)
    branch_settings: dict[Literal["gas", "top", "bridge", "hollow"], BranchSettings] = Field(
        default_factory=dict
    )
    co_height: float = Field(default=1.85, ge=0.8, le=4)
    co_bond: float = Field(default=1.14, ge=0.9, le=1.5)
    gas_cell: float = Field(default=18, ge=12, le=40)
    bulk_job_id: str | None = Field(default=None, max_length=80)
    incar: dict = Field(
        default_factory=lambda: {"ISIF": 2, "IBRION": 2, "NSW": 100, "EDIFFG": -0.03}
    )
    kmesh: tuple[int, int, int] = (9, 9, 1)

    @model_validator(mode="after")
    def validate_surface(self):
        if set(self.branch_settings) - set(["gas", *self.co_sites]) or (
            self.branch_settings and not self.co_sites
        ):
            raise ValueError("局部参数必须对应方案中实际存在的 CO 计算。")
        if len(set(self.co_sites)) != len(self.co_sites):
            raise ValueError("吸附位点不能重复。")
        if not 0 <= self.fixed_layers < self.layers:
            raise ValueError("固定层数必须小于总层数。")
        if (
            any(n < 1 or n > 12 for n in self.size)
            or self.size[0] * self.size[1] * self.layers > 512
        ):
            raise ValueError("表面大小超出范围（最多512原子）。")
        if any(n < 1 or n > 100 for n in self.kmesh) or self.kmesh[2] != 1:
            raise ValueError("表面 k 点须为 1–100，真空方向取1。")
        return self


class PreviewRequest(StrictModel):
    workflow: Literal["single", "relax_static", "bulk_surface"] = "single"
    surface_plan: SurfacePlan | None = None
    server_id: UUID
    structure: StructureInput
    resources: Resources
    kind: Literal["structure_probe", "vasp"] = "structure_probe"
    code_id: UUID | None = None
    incar: dict = Field(default_factory=dict, max_length=200)
    kmesh: tuple[int, int, int] = (1, 1, 1)
    potential_family: str = Field(default="", max_length=120)
    potential_mapping: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("kmesh")
    @classmethod
    def validate_kmesh(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if any(number < 1 or number > 100 for number in value):
            raise ValueError("k 点网格各维应在 1–100 之间。")
        return value


class SubmitRequest(StrictModel):
    preview_id: UUID


class CodeCreate(StrictModel):
    server_id: UUID
    label: Name | None = None
    name: str = Field(default="VASP", pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,63}$")
    version: Name | None = None
    executable: str = Field(min_length=2, max_length=500)
    environment_script: str = Field(default="", max_length=500)

    @field_validator("executable", "environment_script")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value and (not value.startswith("/") or any(char in value for char in "\n\r\x00")):
            raise ValueError("程序和环境脚本必须使用绝对路径。")
        return value
