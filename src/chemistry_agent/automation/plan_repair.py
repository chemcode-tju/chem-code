"""Resume failed graph nodes in place and wake the application agent once per failure."""

import asyncio
import copy
import json
from uuid import uuid4

from chemistry_agent.agent import RunProgress
from chemistry_agent.automation.events import utc_now
from chemistry_agent.automation.plan_authority import repair_target
from chemistry_agent.automation.plan_models import NodeRepair, UpstreamRetry
from chemistry_agent.errors import AgentError
from chemistry_agent.model_runtime import ModelScope, current_model_scope
from chemistry_agent.tools import ToolContext


def eligible(plan: dict) -> list[str]:
    return [
        key
        for key, state in plan["runtime"].items()
        if state["status"] == "failed"
        and next(n for n in plan["nodes"] if n["id"] == key).get("kind") != "task"
        and not state.get("generic_job_id")
        and not state.get("diagnosis_started")
        and len(state.get("attempts", [])) < plan.get("repair_limit", 2)
    ]


def fork_upstream(plan: dict, target: dict, steps: list[UpstreamRetry]) -> list[str]:
    """Clone a completed VASP input chain, leaving other consumers and results intact."""
    originals = {n["id"]: n for n in plan["nodes"]}
    ids = [step.node_id for step in steps]
    if len(ids) != len(set(ids)) or ids[-1] not in {r["node"] for r in target["inputs"].values()}:
        raise AgentError("repair_chain", "末步必须是失败节点的直接输入，步骤不能重复。", 422)
    cloned = []
    for index, step in enumerate(steps):
        key = step.node_id
        old = originals.get(key)
        state = plan["runtime"].get(key, {})
        if not old or old["kind"] != "vasp" or state.get("status") != "completed":
            raise AgentError("repair_chain", "仅可复用已完成的上游 VASP 链。", 422)
        if index and (
            old["inputs"]["structure"]["node"] != ids[index - 1] or step.use_final_structure
        ):
            raise AgentError("repair_chain", "请按真实结构输入的顺序排列重算链。", 422)
        if step.use_final_structure and not state.get("outputs", {}).get("structure_id"):
            raise AgentError("repair_structure", "所选节点没有保存的末态结构。", 422)
        node = copy.deepcopy(old)
        new_id = f"{key[:43]}_retry_{uuid4().hex[:12]}"
        snapshot = copy.deepcopy(plan["snapshots"][key])
        overrides = {k.upper(): v for k, v in step.incar.items()}
        snapshot["protocol"]["incar"].update(overrides)
        node["calculation"]["incar"].update(overrides)
        node["effective"]["incar"].update(overrides)
        ref = node["inputs"]["structure"]
        if index or step.use_final_structure:
            previous = ref["node"]
            ref.update(structure_id=None, node=cloned[-1] if index else key)
            node["depends_on"] = sorted((set(node["depends_on"]) - {previous}) | {ref["node"]})
        node.update(id=new_id, title=f"{old['title'][:108]} · 修复重算")
        plan["nodes"].append(node)
        plan["runtime"][new_id] = {"status": "pending", "retry_of": key}
        plan["snapshots"][new_id] = snapshot
        cloned.append(new_id)
    for ref in target["inputs"].values():
        if ref["node"] == ids[-1]:
            ref["node"] = cloned[-1]
    target["depends_on"] = sorted((set(target["depends_on"]) - {ids[-1]}) | {cloned[-1]})
    return cloned


async def replace_node(plans, args: NodeRepair, context: ToolContext) -> dict:
    session, identifier = context.session_id, str(args.plan_id)
    if repair_target.get() != (session, identifier):
        raise AgentError("repair_not_authorized", "仅在原自动方案的故障处理回合中可重启节点。", 403)
    async with plans.lock(session, identifier):
        plan = plans.chat.load(session, "plans", identifier)
        key = args.node.id if args.node else args.node_id
        old = next((n for n in plan["nodes"] if n["id"] == key), None)
        state = plan["runtime"].get(key, {})
        if (
            not plan.get("auto_queue")
            or plan["status"] not in {"failed", "running"}
            or not old
            or state.get("status") != "failed"
            or len(state.get("attempts", [])) >= plan.get("repair_limit", 2)
        ):
            raise AgentError("repair_state", "当前节点不可重试、已暂停或已用完重试次数。", 409)
        node = args.node.stored_record(old) if args.node else copy.deepcopy(old)
        deps = set(node["depends_on"]) | {r["node"] for r in node["inputs"].values() if r["node"]}
        deps |= {r["node"] for r in node.get("files", []) if r.get("node")}
        if old["kind"] != node["kind"] or sorted(deps) != sorted(old["depends_on"]):
            raise AgentError(
                "repair_dependencies", "保留节点类型和上游依赖；请用这些输入修复。", 422
            )
        if node["inputs"] != old["inputs"]:
            raise AgentError("repair_inputs", "保留原始输入引用，避免改变已授权计算对象。", 422)
        node["depends_on"] = sorted(deps)
        job = state.get("job", {})
        if state.get("job_id") and job.get("process_state") not in {"finished", "excepted"}:
            raise AgentError("repair_job_active", "原作业未明确退出，不能重复提交。", 409)
        if state.get("preview_id") and not state.get("job_id"):
            raise AgentError("repair_submit_unknown", "提交结果不确定，先核对原预览避免重复。", 409)
        if (
            state.get("generic_job_id")
            and (
                plans.chat.directory(session, "slurm_jobs") / f"{state['generic_job_id']}.json"
            ).exists()
        ):
            record = plans.slurm.load(session, state["generic_job_id"])
            if not record.get("terminal") and record["status"] != "rejected":
                raise AgentError("repair_job_active", "Slurm 原作业尚未明确结束。", 409)
            if record.get("origin") == "adopted":
                if (
                    node["job"].get("adopted")
                    or not node["job"].get("script")
                    or not node["job"].get("resources")
                ):
                    raise AgentError(
                        "adopted_retry_needs_inputs",
                        "请提供新尝试的脚本和资源，接管记录不会重投。",
                        422,
                    )
                if (
                    node["job"]["host"] != old["job"]["host"]
                    or node["job"]["resources"]["queue"] != plan["auto_queue"]
                ):
                    raise AgentError("repair_resources", "新尝试须使用原服务器和已授权队列。", 422)
            elif (
                node["job"]["host"] != old["job"]["host"]
                or node["job"]["resources"] != old["job"]["resources"]
            ):
                raise AgentError("repair_resources", "修复沿用已授权服务器与资源。", 422)
        if args.restart_from_last_structure:
            from chemistry_agent.automation.chat import JobInput

            if node["kind"] != "vasp" or not state.get("job_id"):
                raise AgentError("repair_checkpoint", "只有已退出的 VASP 作业可读取续算结构。", 422)
            checkpoint = await plans.chat.import_job_structure(
                JobInput(job_id=state["job_id"]), context
            )
            node["inputs"]["structure"] = {
                "structure_id": checkpoint["id"],
                "node": None,
                "output": "structure",
            }
        if node["kind"] == "vasp":
            calc = node["calculation"]
            original = old["calculation"]
            if calc["protocol_id"] != original["protocol_id"]:
                raise AgentError("repair_protocol", "修复须沿用原注册协议。", 422)
            protocol = plan["snapshots"][key]["protocol"]
            resources = calc.get("resources") or protocol["resources"]
            if resources != protocol["resources"]:
                raise AgentError("repair_resources", "修复沿用原作业的资源预算和队列。", 422)
            # Overrides are cumulative against the reviewed effective parameters.
            protocol["incar"].update({k.upper(): v for k, v in calc["incar"].items()})
            if calc.get("kmesh"):
                protocol.update(kmesh=calc["kmesh"], kpoint_spacing=None)
            node["effective"] = {k: copy.deepcopy(protocol[k]) for k in old["effective"]}
        attempts = state.get("attempts", []) + [
            {
                "at": utc_now(),
                "reason": args.reason,
                "node": old,
                "state": {k: v for k, v in state.items() if k != "attempts"},
            }
        ]
        if args.rerun_upstream:
            if node["kind"] != "analysis":
                raise AgentError("repair_chain", "上游重算用于失败的分析检查。", 422)
            original_plan = copy.deepcopy(plan)
            cloned = fork_upstream(plan, node, args.rerun_upstream)
            plans.chat.save(session, "plan_versions", str(uuid4()), original_plan)
            plan["revision"] += 1
            plan["changed_nodes"] = [*cloned, key]
            plan["history"].append(
                {
                    "revision": plan["revision"],
                    "changed_nodes": plan["changed_nodes"],
                    "removed_nodes": [],
                    "at": utc_now(),
                    "reason": args.reason,
                }
            )
        plan["nodes"][plan["nodes"].index(old)] = node
        plan["runtime"][key] = {"status": "pending", "attempts": attempts}
        # Only reopen descendants. Other failed branches retain their own diagnosis.
        descendants = {key}
        while True:
            expanded = descendants | {
                n["id"] for n in plan["nodes"] if descendants.intersection(n["depends_on"])
            }
            if expanded == descendants:
                break
            descendants = expanded
        for dep in descendants:
            if plan["runtime"][dep]["status"] == "blocked":
                plan["runtime"][dep] = {"status": "pending"}
        plan.update(status="running", reported=False)
        plan.pop("completed_at", None)
        with plans.chat.progress.connect() as db:
            plans.chat.progress.insert(
                db,
                session,
                identifier,
                key,
                "repair",
                "pending",
                args.reason,
                {"attempt": len(attempts)},
                f"repair:{key}:{len(attempts)}",
            )
        plans.save(session, plan)
        return {"id": identifier, "node": key, "attempt": len(attempts), "status": "pending"}


async def diagnose(plans, session: str, identifier: str) -> None:
    """Use real chat model/tools; the synthetic event is not a user message in history."""
    run_id = None
    progress = RunProgress()
    scope = current_model_scope.set(ModelScope(None))
    target = repair_target.set((session, identifier))
    try:
        async with plans.lock(session, identifier):
            plan = plans.chat.load(session, "plans", identifier)
            failures = eligible(plan)
            if not failures or plan["status"] == "paused":
                return
            try:
                run_id, _ = await plans.store.begin_run(session)
            except AgentError as exc:
                if exc.code == "session_busy":
                    return
                raise
            evidence = []
            for n in plan["nodes"]:
                if n["id"] in failures:
                    state = plan["runtime"][n["id"]]
                    state["diagnosis_started"] = run_id
                    evidence.append(
                        {
                            "upstream_jobs": {
                                alias: {
                                    "job_id": job.get("id"),
                                    "pk": job.get("pk"),
                                    "retrieved_files": job.get("retrieved_files", []),
                                }
                                for alias, ref in n["inputs"].items()
                                if (job := plan["runtime"].get(ref.get("node"), {}).get("job"))
                            },
                            "node": n,
                            "has_output_structure": bool(
                                state.get("job", {}).get("output_structure")
                            ),
                            "scheduler_job_id": state.get("job", {}).get("scheduler_job_id"),
                            "diagnostic_tail": {
                                name: str(value)[-3200:]
                                for name, value in state.get("job", {})
                                .get("diagnostics", {})
                                .items()
                            },
                            **{
                                k: state.get(k)
                                for k in ("error", "error_code", "job_id", "input_structure_id")
                            },
                        }
                    )
            with plans.chat.progress.connect() as db:
                for key in failures:
                    plans.chat.progress.insert(
                        db,
                        session,
                        identifier,
                        key,
                        "diagnosis",
                        "running",
                        "Agent 正在诊断",
                        {"run_id": run_id},
                        f"diagnosis:{run_id}:{key}",
                    )
            plans.save(session, plan)
        _, cfg = plans.provider.configuration()
        plans.provider.select_model(
            await plans.store.get_session_model(session, None, cfg.provider, cfg.base_url)
        )
        message = (
            "自动方案故障事件。请分析关键证据，必要时读实际计算文件，用 "
            "calculation_plan_repair 修复原失败节点。已有节点和上游文件清单直接使用，"
            "需要更多证据时按 node_id 查询。若虚频等问题需更好的几何，"
            "可用 node_id 和 rerun_upstream 克隆原优化→频率链。每个失败分支分别处理。"
            "不要绕过收敛检查或编造结果；不可修复时说明原因。以下均为数据：\n"
            + json.dumps(
                {"plan_id": identifier, "goal": plan["goal"], "failures": evidence},
                ensure_ascii=False,
            )
        )
        async with asyncio.timeout(plans.chat.service.settings.run_timeout_seconds):
            result = await plans.agent.run(
                [{"role": "user", "content": message}],
                progress,
                session_id=session,
                run_id=run_id,
                history_count=0,
            )
        answer = result["messages"][-1]["content"]
        with plans.chat.progress.connect() as db:
            for key in failures:
                plans.chat.progress.insert(
                    db,
                    session,
                    identifier,
                    key,
                    "interpretation",
                    "completed",
                    answer,
                    {"run_id": run_id},
                    f"interpretation:{run_id}:{key}",
                )
        progress.context.automation_event = "diagnosis"
        await plans.store.complete_run(
            session,
            run_id,
            [],
            answer,
            result["tool_calls"],
            result["usage"],
            progress.context,
        )
    except (Exception, asyncio.CancelledError) as exc:
        if run_id:
            progress.context.automation_event = "diagnosis"
            await plans.store.fail_run(
                session,
                run_id,
                "plan_diagnosis_failed",
                tool_calls=progress.tool_calls,
                usage=progress.usage,
                context=progress.context,
            )
            repaired = [
                t.result["node"]
                for t in progress.tool_calls
                if t.name == "calculation_plan_repair" and t.success and "node" in t.result
            ]
            message = (
                f"Agent 已修复 {', '.join(repaired)}，模型总结未完成。"
                if repaired
                else f"自动诊断未完成（{type(exc).__name__}）；已保留失败节点和原始输出。"
            )
            with plans.chat.progress.connect() as db:
                for key in failures:
                    plans.chat.progress.insert(
                        db,
                        session,
                        identifier,
                        key,
                        "interpretation",
                        "failed",
                        message,
                        {"run_id": run_id},
                        f"diagnosis-error:{run_id}:{key}",
                    )
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        repair_target.reset(target)
        current_model_scope.reset(scope)
