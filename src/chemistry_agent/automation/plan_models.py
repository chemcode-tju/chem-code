"""The executable graph is also the reviewable, versioned user plan."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from chemistry_agent.automation.models import Resources, StrictModel


class InputReference(StrictModel):
    structure_id: UUID | None = None
    node: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    output: Literal["structure", "result"] = "structure"

    @model_validator(mode="after")
    def one_source(self):
        if (self.structure_id is None) == (self.node is None):
            raise ValueError("输入须指定 structure_id 或上游 node，二选一。")
        if self.structure_id and self.output != "structure":
            raise ValueError("已保存结构只能引用 structure 输出。")
        return self


class VaspSettings(StrictModel):
    protocol_id: str = Field(min_length=1, max_length=64)
    incar: dict = Field(default_factory=dict, max_length=200)
    kmesh: tuple[int, int, int] | None = None
    resources: Resources | None = None
    require_electronic: bool = True
    require_ionic: bool = False

    @model_validator(mode="after")
    def valid_mesh(self):
        if self.kmesh and any(n < 1 or n > 100 for n in self.kmesh):
            raise ValueError("k 点网格各维应在 1–100 之间。")
        return self


class PlanNode(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["model", "vasp", "analysis"]
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, InputReference] = Field(default_factory=dict)
    code: str = Field(default="", max_length=50000)
    calculation: VaspSettings | None = None
    purpose: str = Field(default="", max_length=1000)
    result_files: list[str] = Field(default_factory=list)
    file_source: Literal["retrieved", "remote"] = "retrieved"

    @model_validator(mode="after")
    def valid_operation(self):
        import re

        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", alias) for alias in self.inputs):
            raise ValueError("输入名称须为小写字母开头的字母、数字、下划线。")
        if self.result_files and self.kind != "analysis":
            raise ValueError("result_files 仅用于分析步骤。")
        if self.kind == "vasp":
            if not self.calculation or self.code or len(self.inputs) != 1:
                raise ValueError("VASP 步骤需要 calculation 和唯一的 structure 输入，不执行代码。")
            source = next(iter(self.inputs.values()))
            if source.output != "structure":
                raise ValueError("VASP 输入必须是结构。")
            # Names such as bulk or clean_slab convey the same single structure.
            self.inputs = {"structure": source}
        elif not self.code or self.calculation is not None:
            raise ValueError("建模/分析步骤需要 Python code，不能含 calculation。")
        if self.code:
            try:
                compile(self.code, f"{self.id}.py", "exec")
            except SyntaxError as exc:
                raise ValueError(f"建模/分析代码语法错误：{exc.msg}，第 {exc.lineno} 行。") from exc
        return self


class PlanDraft(StrictModel):
    plan_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=2000)
    nodes: list[PlanNode] = Field(min_length=1)
    max_parallel: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_graph(self):
        if bool(self.plan_id) != bool(self.expected_revision):
            raise ValueError("修改方案须同时提供 plan_id 和 expected_revision。")
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("步骤 id 不能重复。")
        pending = {}
        for node in self.nodes:
            refs = {ref.node for ref in node.inputs.values() if ref.node}
            dependencies = set(node.depends_on) | refs
            if not dependencies <= nodes.keys() or node.id in dependencies:
                raise ValueError(f"{node.id} 引用了不存在的步骤或自身。")
            for ref in node.inputs.values():
                if ref.node and ref.output == "structure" and nodes[ref.node].kind == "analysis":
                    raise ValueError("分析步骤输出 result，不能当作结构。")
            node.depends_on = sorted(dependencies)
            pending[node.id] = dependencies
        done = set()
        while pending:
            ready = {key for key, deps in pending.items() if deps <= done}
            if not ready:
                raise ValueError("方案依赖不能形成环。")
            done.update(ready)
            pending = {key: deps for key, deps in pending.items() if key not in ready}
        return self


class PlanQuery(StrictModel):
    plan_id: UUID | None = None


class PlanConfirmation(StrictModel):
    revision: int = Field(ge=1)


class PlanControl(StrictModel):
    action: Literal["pause", "resume"]


class PlanProgram(StrictModel):
    code: str = Field(min_length=1, description="Python writes full PlanDraft JSON to result.json")
    previous_id: UUID | None = None


class ProgramSource(StrictModel):
    program_id: UUID | None = None
    first_line: int = Field(default=1, ge=1)
    lines: int = Field(default=30, ge=1, le=200)


class NodeRepair(StrictModel):
    plan_id: UUID
    node: PlanNode
    reason: str = Field(min_length=1, max_length=2000)
