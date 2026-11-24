"""SQLite waits and an inbox sharing the session/run transaction boundary."""

import json
import time
from fnmatch import fnmatchcase
from uuid import NAMESPACE_URL, uuid5

from chemistry_agent.automation.wait_models import WaitCondition, WaitRegister
from chemistry_agent.errors import AgentError
from chemistry_agent.platform.task_agents import delivery_target, task_target


async def init_wait_schema(db) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_waits (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            plan_id TEXT NOT NULL, spec TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, event_id TEXT,
            checked_at REAL NOT NULL DEFAULT 0, observation TEXT, error TEXT
        );
        CREATE INDEX IF NOT EXISTS agent_waits_plan ON agent_waits(session_id,plan_id);
        CREATE TABLE IF NOT EXISTS agent_events (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            plan_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, run_id TEXT, phase TEXT NOT NULL DEFAULT 'model',
            checkpoint TEXT, retries INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0, error TEXT, batch_parent TEXT, tool_intent TEXT
        );
        CREATE INDEX IF NOT EXISTS agent_events_pending
            ON agent_events(session_id,plan_id,status,available_at);
    """)
    async with db.execute("PRAGMA table_info(agent_waits)") as cursor:
        if "task_node_id" not in {row["name"] for row in await cursor.fetchall()}:
            await db.execute("ALTER TABLE agent_waits ADD COLUMN task_node_id TEXT")


def evidence(plan: dict, node_id: str) -> dict:
    state = plan["runtime"][node_id]
    job = state.get("job", {})
    return {
        "node_id": node_id,
        "parent_task": next(
            (n.get("parent_task") for n in plan["nodes"] if n["id"] == node_id), None
        ),
        "outcome": state["status"],
        "attempt": attempt(state),
        "state": {
            "generic_job_id": state.get("generic_job_id"),
            "error": state.get("error"),
            "outputs": state.get("outputs"),
            "scheduler_job_id": job.get("scheduler_job_id"),
            "remote_directory": job.get("remote_directory"),
            "origin": job.get("origin"),
            "scheduler_identity": job.get("identity"),
            "instruction": job.get("instruction"),
        },
    }


def attempt(state: dict) -> str:
    return str(state.get("generic_job_id") or state.get("job_id") or len(state.get("attempts", [])))


def current_event(item: dict, plan: dict) -> bool:
    state = plan["runtime"].get(item["node_id"], {})
    return item["attempt"] == attempt(state) and (
        item.get("type") != "step_ready" or state.get("status") == "pending"
    )


def matches(condition: WaitCondition, plan: dict, observed: dict | None = None) -> bool:
    if condition.op in {"all", "any"}:
        values = (matches(child, plan, observed) for child in condition.conditions)
        return all(values) if condition.op == "all" else any(values)
    state = plan["runtime"].get(condition.node, {})
    if condition.op == "state":
        return state.get("status") in condition.states
    # Only observed output manifests count; a desired filename is not evidence.
    files = (state.get("outputs") or {}).get("files", [])
    if observed:
        sample = observed.get(condition.node, {})
        if sample.get("attempt") == attempt(state):
            files = [*files, *sample.get("files", [])]
    return any(
        fnmatchcase(name, condition.pattern)
        or (name.startswith("attempts/") and fnmatchcase(name.split("/", 2)[-1], condition.pattern))
        for name in files
        if isinstance(name, str)
    )


class WaitLedger:
    def __init__(self, store) -> None:
        self.store = store

    async def register(
        self, session: str, plan: dict, args: WaitRegister, *, task_node: str | None = None
    ) -> dict:
        missing = args.condition.nodes() - plan["runtime"].keys()
        if missing:
            raise AgentError("wait_node_missing", f"方案中不存在节点：{sorted(missing)}", 422)
        key = f"{task_node}:{args.key}" if task_node else args.key
        identifier = str(uuid5(NAMESPACE_URL, f"wait:{session}:{plan['id']}:{key}"))
        spec = args.model_dump_json()
        async with self.store.transaction():
            old = await self.store._one("SELECT * FROM agent_waits WHERE id=?", (identifier,))
            if old and json.loads(old["spec"]) != json.loads(spec):
                raise AgentError(
                    "wait_key_conflict", "此 key 已用于另一等待条件，请使用新 key。", 409
                )
            if not old:
                await self.store.db.execute(
                    "INSERT INTO agent_waits"
                    "(id,session_id,plan_id,spec,status,created_at,task_node_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (identifier, session, plan["id"], spec, "waiting", time.time(), task_node),
                )
        return {"wait_id": identifier, "status": old["status"] if old else "waiting"}

    async def rows(self, session: str, plan_id: str) -> list[dict]:
        async with self.store.lock:
            cursor = await self.store.db.execute(
                "SELECT w.*,e.status AS delivery_status,e.error AS delivery_error,e.run_id "
                "FROM agent_waits w "
                "LEFT JOIN agent_events e ON e.id=w.event_id WHERE w.session_id=? AND w.plan_id=?",
                (session, plan_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def probe_files(self, plans, session: str, plan: dict) -> None:
        """Read-only file observations; failures are unknown, never a successful match."""
        if plan["status"] in {"draft", "paused", "stopping", "stopped"}:
            return
        for row in await self.rows(session, plan["id"]):
            args = WaitRegister.model_validate_json(row["spec"])
            if row["status"] != "waiting" or time.time() < row["checked_at"] + args.poll_seconds:
                continue
            pending = [args.condition]
            patterns: dict[str, list[str]] = {}
            while pending:
                condition = pending.pop()
                pending.extend(condition.conditions)
                if condition.op == "file":
                    patterns.setdefault(condition.node, []).append(condition.pattern)
            if not patterns:
                continue
            observed, errors = {}, []
            for node, globs in patterns.items():
                state = plan["runtime"].get(node, {})
                if not state.get("generic_job_id"):
                    continue
                try:
                    record = plans.slurm.load(session, state["generic_job_id"])
                    result = await plans.slurm.remote(record, "files", patterns=globs)
                    observed[node] = {
                        "attempt": attempt(state),
                        "files": [f["path"] for f in result["files"]],
                        "observed_at": time.time(),
                    }
                except Exception as exc:
                    errors.append(f"{node}: {getattr(exc, 'code', type(exc).__name__)}")
            async with self.store.transaction():
                await self.store.db.execute(
                    "UPDATE agent_waits SET checked_at=?,observation=?,error=? WHERE id=?",
                    (time.time(), json.dumps(observed), "; ".join(errors) or None, row["id"]),
                )

    async def observe(self, session: str, plan: dict) -> None:
        """Atomically freeze a match and its inbox event; never reevaluate matched waits."""
        if plan["status"] in {"draft", "paused", "stopping", "stopped"}:
            return
        assigned_nodes = {
            item["id"] for item in plan["nodes"]
            if (item.get("parent_task") or item["id"]) in plan.get("task_agents", {})
        }
        async with self.store.transaction():
            cursor = await self.store.db.execute(
                "SELECT * FROM agent_waits WHERE session_id=? AND plan_id=?",
                (session, plan["id"]),
            )
            covered = set()
            for row in await cursor.fetchall():
                args = WaitRegister.model_validate_json(row["spec"])
                if not row["task_node_id"] and args.condition.nodes() & assigned_nodes:
                    # A pre-assignment wait belongs to the coordinator. It must not
                    # consume any event now owned by an independent task thread.
                    continue
                if args.replace_default:
                    covered.update(args.condition.nodes())
                if row["status"] not in {"waiting", "matched"}:
                    continue
                if (
                    row["status"] == "waiting"
                    and args.expires_at
                    and args.expires_at <= time.time()
                ):
                    await self.store.db.execute(
                        "UPDATE agent_waits SET status='expired' WHERE id=?", (row["id"],)
                    )
                    continue
                observed = json.loads(row["observation"] or "{}")
                if time.time() > row["checked_at"] + args.poll_seconds * 2:
                    observed = {}
                if row["status"] != "waiting" or not matches(args.condition, plan, observed):
                    continue
                payload = {
                    "instruction": args.instruction,
                    "events": [
                        evidence(plan, node)
                        for node in sorted(witnesses(args.condition, plan, observed))
                    ],
                    "wait_id": row["id"],
                    "file_observations": observed,
                    **({"task_node_id": row["task_node_id"]} if row["task_node_id"] else {}),
                }
                await self._insert(row["id"], session, plan, payload)
                await self.store.db.execute(
                    "UPDATE agent_waits SET status='matched',event_id=? WHERE id=?",
                    (row["id"], row["id"]),
                )
            # A user turn may register a grouping rule after a default event was queued.
            cursor = await self.store.db.execute(
                "SELECT id,payload FROM agent_events WHERE session_id=? AND plan_id=? "
                "AND status='pending' AND run_id IS NULL",
                (session, plan["id"]),
            )
            for queued in await cursor.fetchall():
                body = json.loads(queued["payload"])
                if not body.get("wait_id"):
                    body["events"] = [e for e in body["events"] if e["node_id"] not in covered]
                    await self.store.db.execute(
                        "UPDATE agent_events SET status=?,payload=? WHERE id=?",
                        (
                            "pending" if body["events"] else "superseded",
                            json.dumps(body),
                            queued["id"],
                        ),
                    )
            items = []
            for key, legacy in plan.get("job_events", {}).items():
                node = legacy["node_id"]
                if node in covered:
                    continue
                identifier = str(uuid5(NAMESPACE_URL, f"job-event:{session}:{plan['id']}:{key}"))
                old = await self.store._one("SELECT id FROM agent_events WHERE id=?", (identifier,))
                if not old and legacy["status"] == "pending":
                    items.append((identifier, legacy.get("evidence") or evidence(plan, node)))
            for identifier, item in items:
                state = item.get("state", {})
                if state.get("scheduler_job_id") and not state.get("instruction"):
                    from chemistry_agent.automation.monitor_schedules import slurm_instruction

                    item = {
                        **item,
                        "state": {**state, "instruction": await slurm_instruction(self.store)},
                    }
                await self._insert(identifier, session, plan, {"events": [item]})

    async def _insert(self, identifier: str, session: str, plan: dict, payload: dict) -> None:
        payload.update(plan_id=plan["id"], goal=plan["goal"], auto_queue=plan.get("auto_queue"))
        await self.store.db.execute(
            "INSERT OR IGNORE INTO agent_events(id,session_id,plan_id,payload,status,created_at) "
            "VALUES(?,?,?,?,'pending',?)",
            (identifier, session, plan["id"], json.dumps(payload, ensure_ascii=False), time.time()),
        )

    async def pending(
        self,
        session: str,
        plan: dict,
        *,
        exclude_targets: frozenset[str | None] = frozenset(),
        event_id: str | None = None,
    ) -> dict | None:
        async with self.store.transaction():
            # Runs and events share a DB; a committed answer must never cause another turn.
            await self.store.db.execute(
                "UPDATE agent_events SET status=CASE "
                "WHEN EXISTS(SELECT 1 FROM runs WHERE id=run_id AND status='completed') "
                "THEN 'completed' WHEN phase='model' THEN 'pending' ELSE 'uncertain' END "
                "WHERE session_id=? AND plan_id=? AND status='running' AND NOT EXISTS "
                "(SELECT 1 FROM runs WHERE id=run_id AND status='running')",
                (session, plan["id"]),
            )
            cursor = await self.store.db.execute(
                "SELECT * FROM agent_events WHERE session_id=? AND plan_id=? "
                "AND status IN ('pending','retry_wait') AND available_at<=? ORDER BY created_at",
                (session, plan["id"], time.time()),
            )
            candidates = await cursor.fetchall()
            for index, row in enumerate(candidates):
                if event_id is not None and row["id"] != event_id:
                    continue
                item = dict(row)
                payload = json.loads(item["payload"])
                if not payload.get("wait_id") and not item["checkpoint"]:
                    payload["events"] = [e for e in payload["events"] if current_event(e, plan)]
                    item["payload"] = json.dumps(payload, ensure_ascii=False)
                    await self.store.db.execute(
                        "UPDATE agent_events SET payload=? WHERE id=?",
                        (item["payload"], item["id"]),
                    )
                if not payload["events"] or (
                    not item["checkpoint"]
                    and any(not current_event(e, plan) for e in payload["events"])
                ):
                    await self.store.db.execute(
                        "UPDATE agent_events SET status='superseded' WHERE id=?", (row["id"],)
                    )
                    continue
                target = delivery_target(plan, payload)
                if target in exclude_targets:
                    continue
                if not payload.get("wait_id") and not item["run_id"]:
                    for sibling in candidates[index + 1 :]:
                        other = json.loads(sibling["payload"])
                        if other.get("wait_id") or sibling["run_id"]:
                            continue
                        if any(task_target(plan, e) != target for e in other["events"]):
                            continue
                        if any(not current_event(e, plan) for e in other["events"]):
                            continue
                        payload["events"].extend(other["events"])
                        await self.store.db.execute(
                            "UPDATE agent_events SET status='bundled',batch_parent=? WHERE id=?",
                            (item["id"], sibling["id"]),
                        )
                    item["payload"] = json.dumps(payload, ensure_ascii=False)
                    await self.store.db.execute(
                        "UPDATE agent_events SET payload=? WHERE id=?",
                        (item["payload"], item["id"]),
                    )
                return item
        return None

    async def failure(self, identifier: str, error: BaseException) -> None:
        code = getattr(error, "code", type(error).__name__)
        cause_code = getattr(error, "cause_code", None) or code
        not_sent = getattr(error, "delivery_not_sent", False)
        temporary = cause_code in {
            "provider_rate_limited",
            "provider_timeout",
            "provider_unreachable",
            "provider_unavailable",
            "chatgpt_stream_interrupted",
            "provider_stream_interrupted",
            "provider_incomplete",
            "CancelledError",
        }
        temporary = temporary or (
            cause_code == "provider_error" and getattr(error, "retry_after", None) is not None
        )
        temporary = temporary or (
            not_sent and cause_code in {"codex_session_busy", "antigravity_session_busy"}
        )
        async with self.store.transaction():
            row = await self.store._one("SELECT * FROM agent_events WHERE id=?", (identifier,))
            if row["status"] == "completed":
                return
            # A whole agent turn timing out is not a transient provider timeout.
            # Keep its checkpoint for explicit inspection instead of another costly loop.
            # All transient retries are bounded across the durable event lifetime.
            if row["retries"] >= 3:
                temporary = False
            if not_sent:
                # A persisted native preflight checkpoint is not a dispatched prompt.
                await self.store.db.execute(
                    "UPDATE agent_events SET phase='model',tool_intent=NULL WHERE id=?",
                    (identifier,),
                )
            status = (
                "uncertain" if row["phase"] == "tools" and not not_sent
                else "retry_wait" if temporary else "blocked"
            )
            delay = min(3600, 30 * 2 ** min(row["retries"], 7))
            delay = max(delay, getattr(error, "retry_after", None) or 0)
            await self.store.db.execute(
                "UPDATE agent_events SET status=?,error=?,retries=retries+1,available_at=? "
                "WHERE id=?",
                (status, code, time.time() + delay, identifier),
            )


class EventCheckpoint:
    def __init__(self, store, identifier: str) -> None:
        self.store, self.identifier = store, identifier

    async def load(self) -> dict:
        async with self.store.lock:
            row = await self.store._one(
                "SELECT checkpoint FROM agent_events WHERE id=?", (self.identifier,)
            )
        return json.loads(row["checkpoint"]) if row and row["checkpoint"] else {}

    async def mark_tools(
        self, intent: list[dict] | None = None, *, payload: dict | None = None
    ) -> None:
        async with self.store.transaction():
            await self.store.db.execute(
                "UPDATE agent_events SET phase='tools',tool_intent=?,"
                "checkpoint=COALESCE(?,checkpoint) WHERE id=?",
                (
                    json.dumps(intent or []),
                    json.dumps(payload) if payload else None,
                    self.identifier,
                ),
            )
            if payload:
                await self._publish(payload)

    async def save(self, payload: dict) -> None:
        async with self.store.transaction():
            await self.store.db.execute(
                "UPDATE agent_events SET phase='model',tool_intent=NULL,checkpoint=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), self.identifier),
            )
            await self._publish(payload)

    async def _publish(self, payload: dict) -> None:
        from chemistry_agent.runtime.live import public_steps

        # Publish completed tools and public commentary while the background
        # turn is still running. Never expose the synthetic event prompt.
        public = [
            item["content"]
            for item in payload.get("tail", [])
            if item.get("role") == "assistant" and item.get("content")
        ]
        context = {
            **payload.get("context", {}),
            "automation_event": "job_completion",
            "public_steps": public_steps(payload.get("tail", []), payload.get("calls", [])),
        }
        await self.store.db.execute(
            "UPDATE runs SET tool_calls=?,answer=?,context_info=?,usage=? "
            "WHERE id=(SELECT run_id FROM agent_events WHERE id=?) AND status='running'",
            (
                json.dumps(payload.get("calls", []), ensure_ascii=False),
                "\n\n".join(public) or "后台 Agent 正在准备计算并检查执行条件。",
                json.dumps(context),
                json.dumps(
                    {
                        key: max(0, value - payload.get("usage_baseline", {}).get(key, 0))
                        for key, value in payload.get("usage", {}).items()
                    }
                ),
                self.identifier,
            ),
        )
        from chemistry_agent.store import utc_now

        await self.store.db.execute(
            "UPDATE sessions SET updated_at=? WHERE id="
            "(SELECT r.session_id FROM agent_events e JOIN runs r ON r.id=e.run_id WHERE e.id=?)",
            (utc_now(), self.identifier),
        )


def witnesses(condition: WaitCondition, plan: dict, observed: dict) -> set[str]:
    """Freeze only matching observations, not unrelated pending branches."""
    if not matches(condition, plan, observed):
        return set()
    if condition.node:
        return {condition.node}
    return set().union(*(witnesses(child, plan, observed) for child in condition.conditions))
