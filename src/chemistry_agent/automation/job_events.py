"""Persist terminal events and wake the real tool-using agent in the owning session."""

import asyncio
import json
from contextvars import ContextVar

from chemistry_agent.agent import RunProgress
from chemistry_agent.automation.plan_authority import auto_queue, auto_request, repair_target
from chemistry_agent.automation.wait_ledger import (
    EventCheckpoint,
    WaitLedger,
)
from chemistry_agent.automation.wait_ledger import (
    evidence as node_evidence,
)
from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ModelScope, current_model_scope
from chemistry_agent.runtime.checkpoint import current_checkpoint
from chemistry_agent.runtime.shell import shell_grant

job_event_target: ContextVar[tuple[str, str] | None] = ContextVar("job_event_target", default=None)


def record_events(plan: dict) -> None:
    events = plan.setdefault("job_events", {})
    for node in plan["nodes"]:
        state = plan["runtime"][node["id"]]
        if node["kind"] != "job" or state["status"] not in {"completed", "failed"}:
            continue
        key = state.get("generic_job_id") or f"{node['id']}:{len(state.get('attempts', []))}"
        events.setdefault(
            key,
            {
                "status": "pending",
                "node_id": node["id"],
                "outcome": state["status"],
                "evidence": node_evidence(plan, node["id"]),
            },
        )


async def wake(plans, session: str, identifier: str) -> None:
    progress = RunProgress()
    run_id = None
    scope = current_model_scope.set(ModelScope(None))
    event_scope = job_event_target.set((session, identifier))
    tokens = []
    event = None
    ledger = WaitLedger(plans.store)
    try:
        async with plans.lock(session, identifier):
            plan = plans.chat.load(session, "plans", identifier)
            if plan["status"] in {"paused", "stopping", "stopped"}:
                return
            await ledger.observe(session, plan)
            event = await ledger.pending(session, plan)
            if event is None:
                return
            async with plans.store.lock:
                owner = await plans.store._one(
                    "SELECT owner_id FROM sessions WHERE id=?", (session,)
                )
            current_model_scope.set(ModelScope(owner["owner_id"]))
            try:
                run_id, history = await plans.store.begin_run(
                    session, owner["owner_id"], event_id=event["id"]
                )
            except AgentError as exc:
                if exc.code in {"session_busy", "event_already_claimed"}:
                    return
                raise
            payload = json.loads(event["payload"])
            evidence = payload["events"]
        _, config = plans.provider.configuration()
        plans.provider.select_model(
            await plans.store.get_session_model(
                session, owner["owner_id"], config.provider, config.base_url
            )
        )
        hosts = {
            n["job"]["host"]
            for n in plan["nodes"]
            if n["id"] in {e["node_id"] for e in evidence} and n.get("job")
        }
        tokens = [
            (shell_grant, shell_grant.set(next(iter(hosts)) if len(hosts) == 1 else None)),
            (auto_queue, auto_queue.set(plan.get("auto_queue"))),
            (auto_request, auto_request.set("job-event:" + event["id"])),
            (repair_target, repair_target.set((session, identifier))),
            (current_checkpoint, current_checkpoint.set(EventCheckpoint(plans.store, event["id"]))),
        ]
        message = (
            "后台任务结束事件（不是用户的新消息）。请检查真实输出，分析完成或失败原因，"
            "并在原方案中继续已授权的目标。可用 calculation_job_files 读取任意输出，"
            "calculation_plan_repair 修复失败节点，calculation_plan_extend 添加后续步骤。"
            "不要重复提交已接单任务，不把退出码0当成科学结论。自动执行授权范围见 auto_queue；"
            "没有自动授权时提出下一步建议/草稿。隐藏本事件提示词，只向用户呈现分析和行动。\n"
            + json.dumps(
                {
                    "event_id": event["id"],
                    "event_type": "task_event",
                    "instruction": payload.get("instruction", "检查结果并继续已授权的目标"),
                    "file_observations": payload.get("file_observations", {}),
                    "plan_id": identifier,
                    "goal": plan["goal"],
                    "auto_queue": plan.get("auto_queue"),
                    "events": evidence,
                    "current_states": {
                        e["node_id"]: plan["runtime"].get(e["node_id"], {}).get("status")
                        for e in evidence
                    },
                },
                ensure_ascii=False,
            )
        )
        async with asyncio.timeout(plans.chat.service.settings.run_timeout_seconds):
            result = await plans.agent.run(
                [*history, {"role": "system", "content": message}],
                progress,
                session_id=session,
                run_id=run_id,
                history_count=len(history),
            )
        answer = result["messages"][-1]["content"]
        progress.context.automation_event = "job_completion"
        # Persist tool calls and answer, never the synthetic user prompt.
        await plans.store.complete_run(
            session,
            run_id,
            result["messages"][
                next(
                    (
                        i + 1
                        for i, m in enumerate(result["messages"])
                        if m.get("content") == message
                    ),
                    len(result["messages"]) - 1,
                ) :
            ],
            answer,
            result["tool_calls"],
            result["usage"],
            progress.context,
        )
        async with plans.lock(session, identifier):
            current = plans.chat.load(session, "plans", identifier)
            for key, legacy in current.get("job_events", {}).items():
                if key in {e["state"].get("generic_job_id") for e in evidence}:
                    legacy.update(status="completed", run_id=run_id)
            plans.save(session, current)
        with plans.chat.progress.connect() as db:
            plans.chat.progress.insert(
                db,
                session,
                identifier,
                None,
                "interpretation",
                "completed",
                answer,
                {"run_id": run_id},
                f"job-event:{run_id}",
            )
    except (Exception, asyncio.CancelledError) as exc:
        if run_id:
            progress.context.automation_event = "job_completion"
            await plans.store.fail_run(
                session,
                run_id,
                "job_event_interrupted",
                tool_calls=progress.tool_calls,
                usage=progress.usage,
                context=progress.context,
            )
            await ledger.failure(event["id"], exc)
            with plans.chat.progress.connect() as db:
                plans.chat.progress.insert(
                    db,
                    session,
                    identifier,
                    None,
                    "interpretation",
                    "failed",
                    "自动分析中断，已保存恢复点；可恢复的模型请求将退避重试，工具结果不明时先等待核对。",
                    {"run_id": run_id},
                    f"job-event:{run_id}",
                )
        raise
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        job_event_target.reset(event_scope)
        current_model_scope.reset(scope)
