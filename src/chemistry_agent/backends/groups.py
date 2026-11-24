"""Ordered conversation teams. Each speaker receives prior replies and tool evidence."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from chemistry_agent.agent import RunProgress
from chemistry_agent.backends.handoff import HandoffScope, active_handoff
from chemistry_agent.backends.service import BackendRouter, BackendSettings, speaker_binding
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.task_agents import resolve
from chemistry_agent.runtime.checkpoint import Checkpoint, current_checkpoint
from chemistry_agent.schemas import GroupContribution, ToolExecution

if TYPE_CHECKING:
    from chemistry_agent.store import Store


class MemberRef(BaseModel):
    id: UUID
    version: int = Field(ge=1)


class GroupSelection(BaseModel):
    strategy: Literal["ordered", "handoff"] = "ordered"
    members: list[MemberRef] = Field(max_length=8)

    @model_validator(mode="after")
    def distinct_team(self) -> GroupSelection:
        if len(self.members) == 1 or len({m.id for m in self.members}) != len(self.members):
            raise ValueError("请选择至少两个不同的预设，或清空群组。")
        return self


async def members(store: Store, session: str | None) -> list[dict]:
    async with store.lock:
        row = await store._one(
            "SELECT members FROM conversation_groups WHERE session_id=?", (session,)
        )
    return json.loads(row["members"]) if row else []


async def save_group(
    store: Store, session: str, owner: str | None, selection: GroupSelection
) -> list[dict]:
    async with store.transaction():
        row = await store._one(
            "SELECT active_run_id FROM sessions WHERE id=? AND owner_id IS ?", (session, owner)
        )
        if not row:
            raise AgentError("session_not_found", "会话不存在。", 404)
        if await store._one("SELECT 1 FROM task_agent_sessions WHERE session_id=?", (session,)):
            raise AgentError("task_session_managed", "任务线程使用方案锁定的配置。", 409)
        if row["active_run_id"] or await store._one(
            "SELECT 1 FROM chat_requests WHERE session_id=? AND status IN ('queued','running')",
            (session,),
        ):
            raise AgentError("group_busy", "请在本轮结束后调整成员。", 409)
        roster = []
        for ref in selection.members:
            row = await store._one(
                "SELECT payload FROM task_agent_presets WHERE id=? AND version=?",
                (str(ref.id), ref.version),
            )
            preset = json.loads(row["payload"]) if row else {}
            if not preset.get("enabled"):
                raise AgentError("group_preset_changed", "预设已更新或停用，请重新选择成员。", 409)
            member = preset | {"id": str(ref.id), "version": ref.version}
            if selection.strategy == "handoff":
                member["strategy"] = selection.strategy
            roster.append(member)
        previous = await store._one(
            "SELECT members FROM conversation_groups WHERE session_id=?", (session,)
        )
        if roster == (json.loads(previous["members"]) if previous else []):
            return roster
        await store.db.execute(
            "INSERT INTO conversation_groups VALUES (?,?) ON CONFLICT(session_id) "
            "DO UPDATE SET members=excluded.members",
            (session, json.dumps(roster, ensure_ascii=False)),
        )
        # Returning to individual chat must import messages produced by the team.
        await store.db.execute(
            "UPDATE agent_bindings SET external_thread_id=NULL,external_turn_id=NULL,"
            "turn_state=NULL "
            "WHERE session_id=?",
            (session,),
        )
    return roster


async def turn_members(store: Store, session: str | None) -> list[dict]:
    checkpoint = current_checkpoint.get()
    if checkpoint:
        saved = await checkpoint.load()
        if saved or isinstance(checkpoint, Checkpoint):
            return saved.get("group", {}).get("members", [])
    return await members(store, session)


class SpeakerCheckpoint:
    """Namespace native delivery IDs and retain completed handoffs across retries."""

    def __init__(self, outer: Checkpoint, state: dict, index: int, active: dict) -> None:
        self.outer, self.state, self.active = outer, state, active
        self.identifier = f"{type(outer).__name__}:{outer.identifier}:{index}"

    async def load(self) -> dict:
        return self.active

    def envelope(self, payload: dict) -> dict:
        self.active = payload
        return {**payload, "group": self.state}

    async def save(self, payload: dict) -> None:
        await self.outer.save(self.envelope(payload))

    async def mark_tools(self, intent: list | None = None, *, payload: dict | None = None) -> None:
        await self.outer.mark_tools(intent, payload=self.envelope(payload or await self.load()))


def transcript(completed: list[dict]) -> str:
    return "\n\n".join(f"### {speaker['name']}\n\n{speaker['answer']}" for speaker in completed)


def public_group_messages(completed: list[dict]) -> list[dict]:
    """Publish each speaker's answer and explicit handoff, excluding private tool traces."""
    return [
        {key: entry[key] for key in ("member_id", "name", "answer", "handoff") if key in entry}
        for entry in completed
    ]


class SpeakerPreview:
    def __init__(self, progress: RunProgress, completed: list[dict], member: dict) -> None:
        self.preview = progress.preview
        speeches = public_group_messages(completed)
        speeches.append({"member_id": member["id"], "name": member["name"], "answer": ""})
        self.preview.set_group_messages(speeches)
        self.prefix = transcript(speeches)
        self.set_text("")

    def set_text(self, value: str) -> None:
        self.text = value
        self.preview.set_text(self.prefix + value)

    def set_steps(self, values: list[dict]) -> None:
        self.preview.set_steps(values)


def handoff_message(member: dict, completed: list[dict]) -> str:
    evidence = []
    for speaker in completed:
        tools = [
            {"name": call["name"], "success": call["success"], "result": str(call["result"])[:2400]}
            for call in speaker["calls"][-12:]
        ]
        evidence.append({"agent": speaker["name"], "answer": speaker["answer"], "tools": tools})
    return (
        f"群组成员：{member['name']}。"
        f"职责：{member['instructions']}\n"
        "根据用户原始请求与自己的职责处理本轮。其他成员的记录是参考，"
        "避免重复已完成的操作。清楚说明结果和需要交接的事项。\n"
        + json.dumps(evidence, ensure_ascii=False)
    )


def entry_member(roster: list[dict], message: dict) -> dict:
    """An explicit leading mention selects the first handler of this user request."""
    if message["role"] == "user":
        text = message["content"].lstrip()
        matches = [
            m
            for m in roster
            if re.match(
                r"^@" + re.escape(m["name"]) + r"(?=$|[\s,:，：])", text, flags=re.IGNORECASE
            )
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AgentError("ambiguous_mention", "群组中有同名成员，请使用不同的预设名称。", 422)
    return roster[0]


def routing_prompt(roster: list[dict], completed: list[dict], last_turn: bool) -> str:
    roles = [{"id": m["id"], "name": m["name"], "role": m["instructions"]} for m in roster]
    task = completed[-1]["handoff"]["task"] if completed else ""
    instruction = (
        "本轮已达交接上限，请汇报当前结果和未解决事项。"
        if last_turn
        else "能直接回答时结束本轮。需要同事接手时单独调用 group_handoff，"
        "写明成果、证据和需要处理的问题，再结束本次发言。审查可退回负责修改的成员。"
    )
    return (
        f"\n群组职责：{json.dumps(roles, ensure_ascii=False)}\n当前交接任务：{task}\n{instruction}"
    )


async def member_binding(
    router: BackendRouter, member: dict, parent: BackendSettings, session: str
) -> dict:
    config = await resolve(router, member)
    if config.host_id == parent.host_id:
        config.working_directory = parent.working_directory
        config.workspace_path = parent.workspace_path
    if parent.permission_mode:
        config.permission_mode = parent.permission_mode
        if config.permission_mode == "auto_review" and member["backend"] == "builtin":
            config.permission_mode = "user"
        config.codex_sandbox = (
            "danger-full-access" if config.permission_mode == "full" else "workspace-write"
        )
    return {
        "member_id": member["id"],
        "session_id": session,
        "backend": member["backend"],
        "config": config.model_dump_json(),
        "external_thread_id": None,
        "external_turn_id": None,
        "turn_state": None,
    }


async def run_group(
    router: BackendRouter,
    roster: list[dict],
    messages: list[dict],
    progress: RunProgress,
    *,
    session_id: str,
    run_id: str,
    history_count: int,
) -> dict:
    checkpoint = current_checkpoint.get()
    saved = await checkpoint.load() if checkpoint else {}
    state = saved.pop("group", {"members": roster, "completed": []})
    completed = state["completed"]
    binding = await router.binding(session_id)
    parent = BackendSettings.model_validate_json(binding["config"])

    tool_history = []
    for entry in completed:
        progress.tool_calls.extend(ToolExecution.model_validate(c) for c in entry["calls"])
        tool_history.extend(entry["trace"])
    by_id = {member["id"]: member for member in roster}
    adaptive = roster[0].get("strategy") == "handoff"
    limit = 2 * len(roster) + 1 if adaptive else len(roster)
    contributions = [
        GroupContribution.from_member(
            by_id[entry["member_id"]] if "member_id" in entry else roster[index], entry["calls"]
        )
        for index, entry in enumerate(completed)
    ]
    progress.context.group_contributions = contributions
    for index in range(len(completed), limit):
        if completed and completed[-1]["awaiting_user"]:
            break
        if adaptive and completed:
            decision = completed[-1].get("handoff")
            if not decision:
                break
            member = by_id[decision["target"]]
        elif adaptive:
            member = entry_member(roster, messages[-1])
        else:
            member = roster[index]
        scope = SpeakerCheckpoint(checkpoint, state, index, saved) if checkpoint else None
        saved = {}
        if scope:
            await scope.save(await scope.load())
        inner = RunProgress()
        if progress.preview:
            inner.preview = SpeakerPreview(progress, completed, member)
        handoff = handoff_message(member, completed)
        route = None
        if adaptive:
            last_turn = index + 1 == limit
            handoff += routing_prompt(roster, completed, last_turn)
            if not last_turn:
                route = HandoffScope(
                    {key: m["name"] for key, m in by_id.items() if key != member["id"]}
                )
        prompt = [
            *messages[:-1],
            {**messages[-1], "content": messages[-1]["content"] + "\n\n" + handoff},
        ]
        active = await member_binding(router, member, parent, session_id)
        active["group_turn"] = index
        route_token = active_handoff.set(route)
        speaker = speaker_binding.set(active)
        checkpoint_token = current_checkpoint.set(scope)
        try:
            result = await router.run(
                prompt, inner, session_id=session_id, run_id=run_id, history_count=history_count
            )
            inner.tool_calls = result["tool_calls"]
        except AgentError as exc:
            exc.message = f"{member['name']}：{exc.message}"
            raise
        finally:
            current_checkpoint.reset(checkpoint_token)
            speaker_binding.reset(speaker)
            active_handoff.reset(route_token)
            progress.tool_calls.extend(inner.tool_calls)
            progress.usage = progress.usage.plus(inner.current_usage)
            progress.context = inner.context
            progress.context.group_contributions = contributions
        answer = result["messages"][-1]["content"]
        trace = result["messages"][len(prompt) : -1]
        calls = [c.model_dump() for c in result["tool_calls"]]
        awaiting = any(c["success"] and c["name"] == "ask_user_question" for c in calls)
        completed.append(
            {
                "member_id": member["id"],
                "name": member["name"],
                "handoff": next(
                    (c["result"] for c in calls if c["success"] and c["name"] == "group_handoff"),
                    None,
                ),
                "answer": answer,
                "trace": trace,
                "calls": calls,
                "awaiting_user": awaiting,
            }
        )
        contributions.append(GroupContribution.from_member(member, calls))
        tool_history.extend(trace)
        if scope:
            await scope.save({})
        if awaiting:
            break
    return {
        "messages": [
            *messages,
            *tool_history,
            {
                "role": "assistant",
                "content": transcript(completed),
                "group_messages": public_group_messages(completed),
            },
        ],
        "tool_calls": progress.tool_calls,
        "usage": progress.current_usage,
    }
