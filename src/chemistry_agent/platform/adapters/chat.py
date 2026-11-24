"""Translate platform events into the existing conversational Agent protocol."""

import asyncio
import json
from functools import partial

from chemistry_agent.agent import RunProgress
from chemistry_agent.backends.groups import run_group, turn_members
from chemistry_agent.backends.service import BackendRouter
from chemistry_agent.model_runtime import ModelScope, current_model_scope
from chemistry_agent.platform.contracts import Delivery, DeliveryResult
from chemistry_agent.platform.event_context import build_wakeup_message
from chemistry_agent.platform.task_agents import current_task_agent
from chemistry_agent.runtime.shell import shell_grant
from chemistry_agent.store import utc_now


class ChatAgentAdapter:
    def __init__(self, plans) -> None:
        self.plans = plans

    def available(self) -> bool:
        return all(
            value is not None for value in (self.plans.store, self.plans.agent, self.plans.provider)
        )

    async def handle(self, delivery: Delivery) -> DeliveryResult:
        plans = self.plans
        session = delivery.session_id
        plan, payload = delivery.plan, delivery.payload
        run_id, history = delivery.run_id, delivery.history
        evidence = payload["events"]
        progress = RunProgress()
        progress.context.automation_event = "job_completion"
        async with plans.store.transaction():
            await plans.store.db.execute(
                "UPDATE runs SET answer=?,context_info=? WHERE id=? AND status='running'",
                (
                    "后台 Agent 已开始处理已确认的步骤，正在检查输入与执行条件。",
                    json.dumps({"automation_event": "job_completion"}),
                    run_id,
                ),
            )
            await plans.store.db.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (utc_now(), session)
            )
        scope = current_model_scope.set(ModelScope(delivery.owner_id))
        tokens = []
        try:
            _, config = plans.provider.configuration()
            plans.provider.select_model(
                await plans.store.get_session_model(
                    session, delivery.owner_id, config.provider, config.base_url
                )
            )
            hosts = {
                (n.get("job") or {}).get("host") or n.get("execution_host")
                for n in plan["nodes"]
                if n["id"] in {e["node_id"] for e in evidence}
            }
            hosts.discard(None)
            tokens = [
                (shell_grant, shell_grant.set(next(iter(hosts)) if len(hosts) == 1 else None)),
            ]
            message = build_wakeup_message(plan, delivery.event["id"], payload)
            task = current_task_agent.get()
            if task:
                preset = plan["task_agents"][task.node_id]
                message += (
                    f"\n你负责方案 {task.plan_id} 的研究步骤 {task.node_id}。"
                    "请读取本步骤，Slurm 提交使用 calculation_task_submit，"
                    "使用稳定的 key 表示一次具体的提交意图。"
                    "提交由平台排队。等待子作业终态时结束本轮，终态会自动送回此会话。"
                    "NEB 提交用 results=neb，并将已确认的 Fmax 阈值写入 neb_force_target。"
                    "未确认阈值时请请求用户确定，提交不明时等待原回执。"
                    "分析结束后调用 calculation_step_report，明确是否满足科学判据。"
                )
                if preset["instructions"]:
                    message += f"\n执行预设指令：{preset['instructions']}"
            async with asyncio.timeout(plans.chat.service.settings.run_timeout_seconds):
                group = (
                    await turn_members(plans.store, session)
                    if not task and isinstance(plans.agent, BackendRouter)
                    else []
                )
                execute = partial(run_group, plans.agent, group) if group else plans.agent.run
                result = await execute(
                    [*history, {"role": "system", "content": message}],
                    progress,
                    session_id=session,
                    run_id=run_id,
                    history_count=len(history),
                )
            answer = result["messages"][-1]["content"]
            progress.context.automation_event = "job_completion"
            # Persist tool calls and answer, never the synthetic user prompt.
            return DeliveryResult(
                messages=result["messages"][
                    next(
                        (
                            i + 1
                            for i, m in enumerate(result["messages"])
                            if m.get("content") == message
                        ),
                        len(result["messages"]) - 1,
                    ) :
                ],
                answer=answer,
                tool_calls=result["tool_calls"],
                usage=result["usage"],
            )
        finally:
            delivery.tool_calls = progress.tool_calls
            delivery.usage = progress.current_usage
            delivery.context = progress.context
            for variable, token in reversed(tokens):
                variable.reset(token)
            current_model_scope.reset(scope)
