"""An interactive decision closes the current turn and resumes via a durable user reply."""

from typing import Annotated
from uuid import uuid4

from pydantic import Field, model_validator

from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import StrictModel
from chemistry_agent.tools import Tool, ToolContext

Label = Annotated[str, Field(min_length=1, max_length=180)]
ItemId = Annotated[str, Field(min_length=1, max_length=64)]


class Option(StrictModel):
    label: Label
    description: str = Field(default="", max_length=600)


class Question(StrictModel):
    id: ItemId
    question: str = Field(min_length=1, max_length=800)
    options: list[Option] = Field(default_factory=list, max_length=10)
    multi_select: bool = False

    @model_validator(mode="after")
    def distinct_labels(self):
        if len({option.label for option in self.options}) != len(self.options):
            raise ValueError("选项名称必须不同")
        return self


class Ask(StrictModel):
    questions: list[Question] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def distinct_ids(self):
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("同一组问题的 ID 必须不同")
        return self


class Answer(StrictModel):
    id: ItemId
    selected: list[Label] = Field(default_factory=list, max_length=10)
    custom: str = Field(default="", max_length=1800)


class Reply(StrictModel):
    answers: list[Answer] = Field(min_length=1, max_length=5)

    def message(self, questions: Ask) -> str:
        by_id = {answer.id: answer for answer in self.answers}
        if len(by_id) != len(self.answers) or set(by_id) != {q.id for q in questions.questions}:
            raise AgentError("invalid_answers", "请回答这张卡片上的每个问题。", 422)
        lines = []
        for question in questions.questions:
            answer = by_id[question.id]
            selected = set(answer.selected)
            if (
                not selected
                and not answer.custom
                or len(selected) != len(answer.selected)
                or not selected <= {option.label for option in question.options}
                or len(selected) > 1
                and not question.multi_select
            ):
                raise AgentError("invalid_answers", "请选择有效选项或填写自己的回答。", 422)
            lines.append(
                f"{question.question}\n"
                + "；".join([*answer.selected, *([answer.custom] if answer.custom else [])])
            )
        message = "我对方案的选择\n\n" + "\n\n".join(lines)
        if len(message) > 20000:
            raise AgentError("answer_too_long", "回答超过单条消息长度，请缩短补充文字。", 422)
        return message


def ask_user(args: Ask, context: ToolContext) -> dict:
    from chemistry_agent.automation.plan_authority import repair_target

    if repair_target.get():
        raise AgentError("interactive_chat_only", "后台修复请留下待用户决定的诊断。", 422)
    return {"status": "awaiting_user", "question_id": str(uuid4()), **args.model_dump()}


QUESTION_TOOL = Tool(
    "ask_user_question",
    "Ask the user to choose a research approach or supply missing information. "
    "Use this tool alone. It pauses the conversation until the user replies. "
    "Give distinct options with brief tradeoffs, or leave options empty for free text.",
    Ask,
    ask_user,
    True,
)
