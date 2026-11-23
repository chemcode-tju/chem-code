"""Persistent, job-scoped display registrations, separate from execution state."""

import asyncio
import hashlib
import time
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, FiniteFloat, model_validator

from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.result_reader import result_file
from chemistry_agent.errors import AgentError
from chemistry_agent.store import utc_now


class ResultStructure(StrictModel):
    path: str
    label: str = ""


class ResultSeries(StrictModel):
    label: str
    x_label: str
    y_label: str
    unit: str = ""
    points: list[tuple[FiniteFloat, FiniteFloat]] = Field(default_factory=list)


class ResultSource(StrictModel):
    adapter: Literal["inline", "manifest", "json_paths", "auto"] = "inline"
    path: str | None = None
    structures: list[ResultStructure] = Field(default_factory=list)
    series: list[ResultSeries] = Field(default_factory=list)
    pattern: str | None = None
    image_pattern: str = "image_{index:02d}.vasp"
    fields: dict[str, str] = Field(
        default_factory=lambda: {
            "step": "step",
            "energy": "energy_TOTEN_eV",
            "force": "projected_fmax_eV_A",
            "valid": "all_scf_valid",
        }
    )
    energy_label: str = "TOTEN"
    force_label: str = "NEB 投影 Fmax"
    force_target: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    endpoint_outcars: dict[Literal["initial", "final"], str] = Field(
        default_factory=dict,
        description=(
            "Endpoint OUTCAR paths relative to job root; reference energy and raw atomic Fmax, "
            "never NEB projected force."
        ),
    )

    @model_validator(mode="after")
    def validate_source(self):
        from pathlib import Path

        paths = [s.path for s in self.structures] + list(self.endpoint_outcars.values())
        if self.adapter == "manifest":
            if not self.path:
                raise ValueError("manifest 需要 path")
            paths.append(self.path)
        if self.adapter == "json_paths":
            if not self.pattern or "**" in self.pattern:
                raise ValueError("json_paths 需要非递归的快照 pattern")
            if not all(self.fields.get(key) for key in ("step", "energy", "force")):
                raise ValueError("请映射 step、energy、force 字段")
            paths.extend([self.pattern, self.image_pattern.format(index=0)])
        for path in paths:
            result_file(Path("/registered-job"), path)
        return self


class ResultRegister(StrictModel):
    plan_id: UUID
    node_id: str
    job_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    source: ResultSource


class ResultInspect(StrictModel):
    plan_id: UUID
    node_id: str
    job_id: UUID
    key: str | None = None
    file: str | None = None
    path_step: int | None = Field(default=None, ge=0)


class ResultOperations:
    def __init__(self, plans) -> None:
        self.plans = plans
        self.chat = plans.chat
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}

    def binding(self, session: str, plan_id: str, node_id: str, job_id: str) -> dict:
        plan = self.chat.load(session, "plans", plan_id)
        state = plan.get("runtime", {}).get(node_id, {})
        ids = state.get("jobs", []) + [state.get("generic_job_id")]
        if job_id not in ids:
            raise AgentError("result_job_mismatch", "此作业不属于当前方案步骤。", 404)
        return self.plans.slurm.load(session, job_id)

    def identity(self, args) -> str:
        return str(uuid5(NAMESPACE_URL, f"{args.plan_id}:{args.node_id}:{args.job_id}:{args.key}"))

    async def ensure_job(
        self,
        session: str,
        plan_id: str,
        node_id: str,
        job_id: str,
        hint: str = "auto",
        parent: str | None = None,
        *,
        replace: bool = False,
    ) -> None:
        """Bind display alongside job creation, with no SSH/model/submission call."""
        from types import SimpleNamespace

        from chemistry_agent.automation.result_defaults import display_source

        source = display_source(hint)
        if source["adapter"] == "json_paths":
            record = self.plans.slurm.load(session, job_id)
            source["force_target"] = record.get("settings", {}).get("neb_force_target")
        for target in dict.fromkeys([node_id, *([parent] if parent else [])]):
            args = ResultRegister(
                plan_id=plan_id,
                node_id=target,
                job_id=job_id,
                key="job-results",
                title="计算结果",
                source=source,
            )
            if not replace:
                try:
                    self.chat.load(session, "result_registry", self.identity(args))
                    continue
                except AgentError as exc:
                    if exc.status_code != 404:
                        raise
            await self.register(args, SimpleNamespace(session_id=session))

    async def register(self, args: ResultRegister, context) -> dict:
        self.binding(context.session_id, str(args.plan_id), args.node_id, str(args.job_id))
        identifier = self.identity(args)
        # Only this display record is written; never rewrite the plan or scheduler record.
        payload = args.model_dump(mode="json")
        try:
            previous = self.chat.load(context.session_id, "result_registry", identifier)
        except AgentError as exc:
            if exc.status_code != 404:
                raise
        else:
            if all(previous.get(key) == value for key, value in payload.items()):
                return previous
        data = payload | {"id": identifier, "updated_at": utc_now()}
        self.chat.save(context.session_id, "result_registry", identifier, data)
        self.cache.clear()
        return data

    async def inspect(self, args: ResultInspect, context) -> dict:
        session = context.session_id
        job = self.binding(session, str(args.plan_id), args.node_id, str(args.job_id))
        records = [
            r
            for r in self.chat.list_records(session, "result_registry", limit=10000)
            if r["plan_id"] == str(args.plan_id)
            and r["node_id"] == args.node_id
            and r["job_id"] == str(args.job_id)
        ]
        if args.key is None:
            records.sort(key=lambda row: row["source"]["adapter"] == "auto")
            return {"datasets": [{"key": r["key"], "title": r["title"]} for r in records]}
        registration = next((r for r in records if r["key"] == args.key), None)
        if registration is None:
            raise AgentError("result_not_found", "没有该结果登记。", 404)
        revision = hashlib.sha256(str(registration).encode()).hexdigest()[:16]
        selection = (
            f"path-step:{args.path_step}" if args.path_step is not None else args.file or "index"
        )
        suffix = hashlib.sha256(selection.encode()).hexdigest()[:24]
        cache_id = str(uuid5(NAMESPACE_URL, f"{registration['id']}:{revision}:{suffix}"))
        key = (session, cache_id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            previous = self.cache.get(key)
            if previous and time.monotonic() - previous[0] < 20:
                return previous[1]
            try:
                data = await self.plans.slurm.remote(
                    job,
                    "result_snapshot",
                    source=registration["source"],
                    file=args.file,
                    **({"path_step": args.path_step} if args.path_step is not None else {}),
                )
                data.update(
                    title=registration["title"],
                    key=args.key,
                    job_id=str(args.job_id),
                    observed_at=utc_now(),
                    stale=False,
                )
                self.chat.save(session, "result_snapshots", cache_id, data)
            except AgentError as exc:
                try:
                    data = self.chat.load(session, "result_snapshots", cache_id)
                except AgentError:
                    raise exc from None
                data = data | {"stale": True, "read_error": exc.message}
            self.cache[key] = (time.monotonic(), data)
            return data
