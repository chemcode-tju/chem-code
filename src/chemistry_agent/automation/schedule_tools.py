"""Conversation-scoped Agent operations on the same timers as the monitor panel."""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.monitor_schedules import Schedule, ScheduleFields, write_schedule
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext


class ScheduleSet(ScheduleFields):
    key: str | None = Field(default=None, min_length=1, max_length=120)
    schedule_id: UUID | None = None

    @model_validator(mode="after")
    def require_target(self) -> "ScheduleSet":
        if bool(self.key) == bool(self.schedule_id):
            raise ValueError("创建时指定稳定的 key，编辑时指定 schedule_id。")
        return self


class ScheduleControl(StrictModel):
    action: Literal["list", "pause", "resume", "delete"] = "list"
    schedule_id: UUID | None = None

    @model_validator(mode="after")
    def require_identifier(self) -> "ScheduleControl":
        if (self.action == "list") != (self.schedule_id is None):
            raise ValueError("管理单个任务时需要 schedule_id。")
        return self


def tools(plans) -> list[Tool]:
    async def save(args: ScheduleSet, context: ToolContext) -> dict:
        session = UUID(context.session_id)
        identifier = args.schedule_id or uuid5(session, f"monitor-schedule:{args.key}")
        store = plans.store
        async with store.transaction():
            old = await store._one("SELECT * FROM monitor_schedules WHERE id=?", (str(identifier),))
            if (args.schedule_id and not old) or (old and old["session_id"] != str(session)):
                raise AgentError("schedule_not_found", "当前对话中没有该定时任务。", 404)
            spec = Schedule(
                **args.model_dump(exclude={"key", "schedule_id"}),
                session_id=session,
                enabled=bool(old["enabled"]) if old else True,
            )
            await write_schedule(store, identifier, spec, created_by="agent")
            row = await store._one("SELECT * FROM monitor_schedules WHERE id=?", (str(identifier),))
        return dict(row)

    async def control(args: ScheduleControl, context: ToolContext) -> dict:
        store = plans.store
        async with store.transaction():
            if args.action == "list":
                async with store.db.execute(
                    "SELECT m.*,q.status AS last_status FROM monitor_schedules m "
                    "LEFT JOIN chat_requests q ON m.last_request=q.id WHERE m.session_id=? "
                    "ORDER BY m.rowid DESC",
                    (context.session_id,),
                ) as cursor:
                    return {"schedules": [dict(row) for row in await cursor.fetchall()]}
            target = (str(args.schedule_id), context.session_id)
            row = await store._one(
                "SELECT * FROM monitor_schedules WHERE id=? AND session_id=?", target
            )
            if not row:
                raise AgentError("schedule_not_found", "当前对话中没有该定时任务。", 404)
            if args.action == "delete":
                await store.db.execute(
                    "DELETE FROM monitor_schedules WHERE id=? AND session_id=?", target
                )
                return {"id": str(args.schedule_id), "deleted": True}
            body = Schedule(
                **{field: row[field] for field in Schedule.model_fields}
                | {"enabled": args.action == "resume"}
            )
            await write_schedule(store, args.schedule_id, body)
            return {"id": str(args.schedule_id), "enabled": body.enabled}

    return [
        Tool(
            "monitor_schedule_set",
            "Create a recurring Agent wake in THIS conversation without a plan. "
            "Use a stable key for creation and retries, or schedule_id to edit. "
            "Give an interval in minutes and an instruction. Editing preserves paused state.",
            ScheduleSet,
            save,
            True,
        ),
        Tool(
            "monitor_schedule_control",
            "List this conversation's recurring wakes, or pause, resume "
            "or delete one by schedule_id. Does not alter its calculation plans.",
            ScheduleControl,
            control,
            True,
        ),
    ]
