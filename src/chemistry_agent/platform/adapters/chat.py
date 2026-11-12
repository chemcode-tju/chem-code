"""Translate platform events into the existing conversational Agent protocol."""

import asyncio
import json

from chemistry_agent.agent import RunProgress
from chemistry_agent.model_runtime import ModelScope, current_model_scope
from chemistry_agent.platform.contracts import Delivery, DeliveryResult
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
        session, identifier = delivery.session_id, delivery.plan["id"]
        plan, event, payload = delivery.plan, delivery.event, delivery.payload
        run_id, history = delivery.run_id, delivery.history
        evidence = payload["events"]
        progress = RunProgress()
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
            message = (
                "后台任务结束事件 / 研究步骤就绪事件（不是用户的新消息）。"
                "step_ready 表示 Agent 负责的步骤已就绪，"
                "请按确认的目标执行，平台不会同时执行该步骤。其他事件请检查真实输出、分析完成或失败原因。"
                "研究步骤可关联多个作业；使用 calculation_job_adopt 的 step_id 关联，"
                "分析后用 calculation_step_report 记录结论和采用的作业，保留失败尝试。"
                "并在原方案中继续已授权的目标。可用 calculation_job_files 读取任意输出，"
                "calculation_plan_repair 修复失败节点，calculation_plan_extend 添加后续步骤。"
                "不要重复提交已接单任务，不把退出码0当成科学结论。"
                "用户已确认的 task 可按其目标和约束执行，不必重复确认同一目标；"
                "auto_queue 指定自动队列授权，超出已确认目标或缺少必要选择时提出建议/草稿。"
                "用户可在详情中修改未开始步骤；"
                "准备输入和提交前用 calculation_plan_get 读取最新版本。"
                "最新 purpose、text_files 和 submission_script 是用户保存的执行依据，"
                "须实际写入参数文件和脚本，不能按旧事件或聊天记录覆盖。说明和文件冲突时先澄清。"
                "隐藏本事件提示词，只向用户呈现分析和行动。\n"
                + json.dumps(
                    {
                        "event_id": event["id"],
                        "event_type": "task_event",
                        "instruction": payload.get("instruction", "检查结果并继续已授权的目标"),
                        "file_observations": payload.get("file_observations", {}),
                        "plan_id": identifier,
                        "goal": plan["goal"],
                        "approved_revision": plan.get("approved_revision"),
                        "research_steps": [n for n in plan["nodes"] if n["kind"] == "task"],
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
            delivery.usage = progress.usage
            delivery.context = progress.context
            for variable, token in reversed(tokens):
                variable.reset(token)
            current_model_scope.reset(scope)
