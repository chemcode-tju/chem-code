from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SessionCreate(StrictModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)


class SessionModelUpdate(StrictModel):
    model: str | None = Field(..., min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$")


class ChatRequest(StrictModel):
    model: str | None = Field(
        default=None, min_length=1, max_length=160, pattern=r"^[^\x00-\x20\x7f]+$"
    )
    file_ids: list[UUID] = Field(default_factory=list)
    message: str = Field(min_length=1, max_length=20000)
    session_id: UUID | None = None
    # User-supplied excerpts only; this endpoint does not read local files or fetch URLs.
    context: str | None = Field(default=None, max_length=50000)
    structure_ids: list[UUID] = Field(default_factory=list)


class QueuedChatRequest(ChatRequest):
    session_id: UUID
    request_id: UUID


class TokenUsage(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            **{name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields}
        )


class ToolExecution(BaseModel):
    call_id: str
    name: str
    arguments: Any
    success: bool
    result: dict[str, Any]


class GroupContribution(BaseModel):
    preset_id: str
    version: int
    name: str
    instance_id: str
    model: str
    call_ids: list[str]

    @classmethod
    def from_member(cls, member: dict, calls: list[dict]) -> "GroupContribution":
        return cls(
            preset_id=member["id"],
            version=member["version"],
            name=member["name"],
            instance_id=member["instance_id"],
            model=member["model"],
            call_ids=[call["call_id"] for call in calls],
        )


class ContextUsage(BaseModel):
    group_contributions: list[GroupContribution] = Field(default_factory=list)
    automation_event: Literal["diagnosis", "submission", "job_completion"] | None = None
    error_message: str | None = None
    manual_compaction: bool = False
    compressed: bool = False
    summary_used: bool = False
    summarized_messages: int = 0
    retained_turns: int = 0
    compacted_tool_results: int = 0
    input_tokens_before: int = 0
    input_tokens_after: int = 0
    input_budget_tokens: int = 0
    trigger_tokens: int = 0
    tokenizer: str = ""
    summary_calls: int = 0
    summary_attempts: int = 0
    summary_error_code: str | None = None
    summary_error_stage: str | None = None
    summary_input_tokens: int = 0
    summary_output_size: int = 0
    summary_memory_budget: int = 0
    summary_deferred: bool = False
    summary_usage: TokenUsage = Field(default_factory=TokenUsage)


class ContextMemory(BaseModel):
    summary: str = ""
    covered_messages: int = 0


class ChatResponse(BaseModel):
    session_id: UUID
    run_id: UUID
    answer: str
    tool_calls: list[ToolExecution]
    usage: TokenUsage
    context: ContextUsage = Field(default_factory=ContextUsage)


class SessionSummary(BaseModel):
    id: UUID
    title: str
    created_at: str
    updated_at: str


class SessionDetail(SessionSummary):
    title_pending: bool = False
    messages: list[dict[str, Any]]


class RunDetail(BaseModel):
    id: UUID
    session_id: UUID
    status: Literal["running", "completed", "failed", "interrupted", "cancelled"]
    started_at: str
    finished_at: str | None
    answer: str | None
    error_code: str | None
    tool_calls: list[ToolExecution]
    usage: TokenUsage
    context: ContextUsage = Field(default_factory=ContextUsage)
