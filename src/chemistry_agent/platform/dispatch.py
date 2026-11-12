"""Offer persisted events to replaceable consumers without knowing a model provider."""

import asyncio
import json
import logging
from contextlib import suppress

from chemistry_agent.automation.plan_authority import auto_queue, auto_request, repair_target
from chemistry_agent.automation.wait_ledger import EventCheckpoint, WaitLedger
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import Delivery, DeliveryAdapter, job_event_target
from chemistry_agent.runtime.checkpoint import current_checkpoint

logger = logging.getLogger(__name__)


class SessionDispatcher:
    def __init__(self, plans) -> None:
        self.plans = plans
        self.adapters: dict[str, DeliveryAdapter] = {}
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.worker: asyncio.Task | None = None

    def register(self, name: str, adapter: DeliveryAdapter) -> None:
        self.adapters[name] = adapter

    async def offer(self, session: str, plan: dict) -> None:
        adapter = self.adapters.get(plan.get("delivery_adapter", "chat"))
        key = (session, plan["id"])
        if (
            not self.plans.store
            or not adapter
            or not adapter.available()
            or plan["status"] in {"draft", "paused", "stopping", "stopped"}
            or key in self.tasks
        ):
            return
        if await WaitLedger(self.plans.store).pending(session, plan) is None:
            return
        task = asyncio.create_task(self.deliver_now(*key, plan.get("delivery_adapter", "chat")))
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

    async def deliver_now(self, session: str, plan_id: str, adapter_name: str) -> None:
        plans, event, run_id, delivery = self.plans, None, None, None
        ledger = WaitLedger(plans.store)
        adapter = self.adapters.get(adapter_name)
        if not adapter or not adapter.available():
            return
        tokens = []
        try:
            async with plans.lock(session, plan_id):
                plan = plans.chat.load(session, "plans", plan_id)
                if plan["status"] in {"draft", "paused", "stopping", "stopped"}:
                    return
                if plan.get("delivery_adapter", "chat") != adapter_name:
                    return
                await ledger.observe(session, plan)
                event = await ledger.pending(session, plan)
                if event is None:
                    return
                async with plans.store.lock:
                    owner = await plans.store._one(
                        "SELECT owner_id FROM sessions WHERE id=?", (session,)
                    )
                try:
                    run_id, history = await plans.store.begin_run(
                        session, owner["owner_id"], event_id=event["id"]
                    )
                except AgentError as exc:
                    if exc.code in {"session_busy", "event_already_claimed"}:
                        return
                    raise
                delivery = Delivery(
                    session,
                    plan,
                    event,
                    json.loads(event["payload"]),
                    run_id,
                    history,
                    owner["owner_id"],
                )
            tokens = [
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
                session,
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
                    None,
                    "interpretation",
                    "completed",
                    result.answer,
                    {"run_id": run_id, "adapter": adapter_name},
                    f"job-event:{run_id}",
                )
        except (Exception, asyncio.CancelledError) as exc:
            if run_id:
                await plans.store.fail_run(
                    session,
                    run_id,
                    getattr(exc, "code", "job_event_interrupted"),
                    tool_calls=delivery.tool_calls,
                    usage=delivery.usage,
                    context=delivery.context,
                )
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
