"""Offer persisted events to replaceable consumers without knowing a model provider."""

import asyncio
import json
import logging
from contextlib import suppress

from chemistry_agent.automation.plan_authority import auto_queue, auto_request, repair_target
from chemistry_agent.automation.wait_ledger import EventCheckpoint, WaitLedger
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import Delivery, DeliveryAdapter, job_event_target
from chemistry_agent.platform.task_agents import (
    TaskAgentScope,
    current_task_agent,
    delivery_target,
    ensure_session,
)
from chemistry_agent.runtime.checkpoint import current_checkpoint

logger = logging.getLogger(__name__)
MAX_BACKGROUND_RUNS = 4


class SessionDispatcher:
    def __init__(self, plans) -> None:
        self.plans = plans
        self.adapters: dict[str, DeliveryAdapter] = {}
        self.tasks: dict[tuple[str, str, str | None], asyncio.Task] = {}
        self.offer_lock = asyncio.Lock()
        self.worker: asyncio.Task | None = None

    def register(self, name: str, adapter: DeliveryAdapter) -> None:
        self.adapters[name] = adapter

    def tasks_for(self, session: str, plan_id: str) -> set[asyncio.Task]:
        return {
            task for (owner, plan, _), task in self.tasks.items()
            if (owner, plan) == (session, plan_id)
        }

    async def offer(self, session: str, plan: dict) -> None:
        adapter_name = plan.get("delivery_adapter", "chat")
        adapter = self.adapters.get(adapter_name)
        plan_id = plan["id"]
        if (
            not self.plans.store
            or not adapter
            or not adapter.available()
            or plan["status"] in {"draft", "paused", "stopping", "stopped"}
        ):
            return
        ledger = WaitLedger(self.plans.store)
        async with self.offer_lock:
            targets = {
                node for owner, pid, node in self.tasks if (owner, pid) == (session, plan_id)
            }
            plan_slots = 1 + len(plan.get("task_agents", {})) - len(targets)
            available_slots = min(MAX_BACKGROUND_RUNS - len(self.tasks), plan_slots)
            # Bound selection even when a busy target frees its slot immediately.
            for _ in range(available_slots):
                event = await ledger.pending(session, plan, exclude_targets=frozenset(targets))
                if event is None:
                    break
                target = delivery_target(plan, json.loads(event["payload"]))
                targets.add(target)
                self.start_delivery((session, plan_id, target), adapter_name, event["id"])

    def start_delivery(
        self, key: tuple[str, str, str | None], adapter_name: str, event_id: str
    ) -> None:
        session, plan_id, _ = key
        task = asyncio.create_task(
            self.deliver_now(session, plan_id, adapter_name, event_id=event_id)
        )
        self.tasks[key] = task

        def finished(task: asyncio.Task) -> None:
            self.tasks.pop(key, None)
            if not task.cancelled() and task.exception():
                error = task.exception()
                logger.error(
                    "Event delivery delayed: code=%s cause=%s",
                    getattr(error, "code", type(error).__name__),
                    getattr(error, "cause_code", None),
                )

        task.add_done_callback(finished)

    async def deliver_now(
        self, session: str, plan_id: str, adapter_name: str, *, event_id: str | None = None
    ) -> None:
        plans, event, run_id, delivery = self.plans, None, None, None
        ledger = WaitLedger(plans.store)
        adapter = self.adapters.get(adapter_name)
        if not adapter or not adapter.available():
            return
        tokens = []
        execution_session = session
        scope = None
        try:
            async with plans.lock(session, plan_id):
                plan = plans.chat.load(session, "plans", plan_id)
                if (
                    plan["status"] in {"draft", "paused", "stopping", "stopped"}
                    or (session, plan_id) in plans.stop_requests
                ):
                    return
                if plan.get("delivery_adapter", "chat") != adapter_name:
                    return
                await ledger.observe(session, plan)
                event = await ledger.pending(session, plan, event_id=event_id)
                if event is None:
                    return
                payload = json.loads(event["payload"])
                async with plans.store.lock:
                    owner = await plans.store._one(
                        "SELECT owner_id FROM sessions WHERE id=?", (session,)
                    )
                node = delivery_target(plan, payload)
                if node:
                    execution_session = await ensure_session(
                        plans.agent, session, plan, node, owner["owner_id"]
                    )
                    scope = TaskAgentScope(session, plan_id, node, execution_session)
                try:
                    run_id, history = await plans.store.begin_run(
                        execution_session, owner["owner_id"], event_id=event["id"],
                        event_session_id=session if scope else None,
                    )
                except AgentError as exc:
                    if exc.code in {"session_busy", "event_already_claimed"}:
                        return
                    raise
                delivery = Delivery(
                    execution_session,
                    plan,
                    event,
                    payload,
                    run_id,
                    history,
                    owner["owner_id"],
                )
            tokens = [
                (current_task_agent, current_task_agent.set(scope)),
                (job_event_target, job_event_target.set((session, plan_id))),
                (auto_queue, auto_queue.set(plan.get("auto_queue"))),
                (auto_request, auto_request.set("job-event:" + event["id"])),
                (repair_target, repair_target.set((session, plan_id))),
                (
                    current_checkpoint,
                    current_checkpoint.set(EventCheckpoint(plans.store, event["id"])),
                ),
            ]
            async with asyncio.timeout(plans.chat.service.settings.run_timeout_seconds):
                result = await adapter.handle(delivery)
            await plans.store.complete_run(
                execution_session,
                run_id,
                result.messages or [{"role": "assistant", "content": result.answer}],
                result.answer,
                result.tool_calls,
                result.usage,
                delivery.context,
            )
            async with plans.lock(session, plan_id):
                current = plans.chat.load(session, "plans", plan_id)
                for item in current.get("job_events", {}).values():
                    if any(
                        e["node_id"] == item["node_id"]
                        and e["attempt"] == item.get("evidence", {}).get("attempt")
                        for e in delivery.payload["events"]
                    ):
                        item.update(status="completed", run_id=run_id)
                plans.save(session, current)
            with plans.chat.progress.connect() as db:
                plans.chat.progress.insert(
                    db,
                    session,
                    plan_id,
                    scope.node_id if scope else None,
                    "interpretation",
                    "completed",
                    "任务 Agent 回合已结束。" if scope else result.answer,
                    {"run_id": run_id, "adapter": adapter_name},
                    f"job-event:{run_id}",
                )
        except (Exception, asyncio.CancelledError) as exc:
            if run_id:
                await plans.store.fail_run(
                    execution_session,
                    run_id,
                    getattr(
                        exc,
                        "code",
                        "agent_turn_timeout"
                        if isinstance(exc, TimeoutError)
                        else "job_event_interrupted",
                    ),
                    tool_calls=delivery.tool_calls,
                    usage=delivery.usage,
                    context=delivery.context,
                    error_message=getattr(exc, "message", None),
                    cancelled=isinstance(exc, asyncio.CancelledError),
                )
            if event:
                await ledger.failure(event["id"], exc)
            raise
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    async def run_once(self) -> None:
        for path in self.plans.chat.root.glob("*/plans/*.json"):
            try:
                session = path.parent.parent.name
                plan = self.plans.chat.load(session, "plans", path.stem)
                await self.offer(session, plan)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Event dispatch delayed")

    async def watch(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(5)

    def start(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.watch())

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
