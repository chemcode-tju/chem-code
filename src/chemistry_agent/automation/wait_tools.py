"""Register, inspect and cancel durable waits from the conversation agent."""

from chemistry_agent.automation.wait_ledger import WaitLedger
from chemistry_agent.automation.wait_models import WaitControl, WaitRegister
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import Tool, ToolContext


def tools(plans) -> list[Tool]:
    async def register(args: WaitRegister, context: ToolContext) -> dict:
        async with plans.lock(context.session_id, str(args.plan_id)):
            plan = plans.chat.load(context.session_id, "plans", str(args.plan_id))
            if plan["status"] in {"stopped", "stopping"}:
                raise AgentError("plan_stopped", "方案已停止，不能注册自动续跑。", 409)
            return await WaitLedger(plans.store).register(context.session_id, plan, args)

    async def control(args: WaitControl, context: ToolContext) -> dict:
        session, plan_id = context.session_id, str(args.plan_id)
        async with plans.lock(session, plan_id):
            plans.chat.load(session, "plans", plan_id)
            ledger = WaitLedger(plans.store)
            if args.action != "list":
                if args.event_id:
                    async with plans.store.lock:
                        found = await plans.store._one(
                            "SELECT COALESCE(p.id,e.id) AS event_id, "
                            "COALESCE(p.status,e.status) AS delivery_status,NULL AS id "
                            "FROM agent_events e LEFT JOIN agent_events p ON p.id=e.batch_parent "
                            "WHERE e.id=? AND e.session_id=? AND e.plan_id=?",
                            (str(args.event_id), session, plan_id),
                        )
                    row = dict(found) if found else None
                else:
                    rows = await ledger.rows(session, plan_id)
                    row = next((r for r in rows if r["id"] == str(args.wait_id)), None)
                if row is None:
                    raise AgentError("wait_not_found", "当前方案中不存在此等待。", 404)
                if row["delivery_status"] == "running":
                    raise AgentError("wait_running", "续跑已开始，请使用方案停止操作。", 409)
                async with plans.store.transaction():
                    if args.action == "cancel":
                        await plans.store.db.execute(
                            "UPDATE agent_waits SET status='cancelled' WHERE id=?", (row["id"],)
                        )
                        await plans.store.db.execute(
                            "UPDATE agent_events SET status='cancelled' "
                            "WHERE (id=? OR batch_parent=?) "
                            "AND status NOT IN ('completed','superseded')",
                            (row["event_id"], row["event_id"]),
                        )
                    else:
                        cursor = await plans.store.db.execute(
                            "UPDATE agent_events SET status='pending',available_at=0 WHERE id=? "
                            "AND phase='model' AND status IN ('blocked','retry_wait')",
                            (row["event_id"],),
                        )
                        if not cursor.rowcount:
                            raise AgentError(
                                "wait_not_replayable",
                                "没有可安全续跑的模型检查点；请先核对工具结果。",
                                409,
                            )
            async with plans.store.lock:
                cursor = await plans.store.db.execute(
                    "SELECT id,status,error,run_id,available_at,phase,tool_intent "
                    "FROM agent_events "
                    "WHERE session_id=? AND plan_id=? ORDER BY created_at",
                    (session, plan_id),
                )
                events = [dict(row) for row in await cursor.fetchall()]
            return {"waits": await ledger.rows(session, plan_id), "events": events}

    return [
        Tool(
            "calculation_wait_register",
            "Persist a one-shot wait in the current plan/session. Compose all/any conditions: "
            "state(node,states) or file(node,pattern). On match, wake this agent with instruction. "
            "Use a stable key for idempotency; new key for a new wait. replace_default groups "
            "covered nodes instead of waking on every completion. No new compute authority. "
            "File conditions observe files, not scientific validity or write completeness.",
            WaitRegister,
            register,
            True,
        ),
        Tool(
            "calculation_wait_control",
            "List durable waits/event recovery status, cancel a wait, "
            "or retry a blocked model boundary after fixing configuration. "
            "Never replays uncertain tools.",
            WaitControl,
            control,
            True,
        ),
    ]
