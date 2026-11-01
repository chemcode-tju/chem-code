"""Graph tests use deterministic jobs; no cluster is contacted or charged."""

import io
import json
from uuid import uuid4

import pytest
from ase.build import bulk
from ase.io import read, write
from pydantic import ValidationError

from chemistry_agent.automation.chat import CalculationChat, ImportInput
from chemistry_agent.automation.models import StructureInput
from chemistry_agent.automation.plan_models import PlanDraft
from chemistry_agent.automation.plans import CalculationPlans
from chemistry_agent.errors import AgentError
from chemistry_agent.tools import ToolContext


def structure_text(lattice=3.6):
    stream = io.StringIO()
    write(stream, bulk("Cu", "fcc", a=lattice, cubic=True), format="vasp")
    return stream.getvalue()


class Engine:
    def __init__(self, settings):
        self.settings = settings
        self.jobs = {}
        self.requests = {}
        self.submits = []
        self.identity = "unchanged"
        self.unavailable = False
        settings.aiida_path.mkdir(parents=True, exist_ok=True)
        self.protocol = {
            "id": "pbe",
            "description": "test protocol",
            "server_id": str(uuid4()),
            "code_id": str(uuid4()),
            "resources": {"queue": "batch", "wallclock_seconds": 86400},
            "potential_source_id": "pbe",
            "incar": {"ENCUT": 520, "NSW": 80},
            "kmesh": [9, 9, 9],
            "max_atoms": 512,
        }
        (settings.aiida_path / "calculation-configuration.json").write_text(
            json.dumps(
                {
                    "protocols": [self.protocol],
                    "sources": [
                        {
                            "id": "pbe",
                            "family": "PBE",
                            "root": "/potentials",
                            "server_id": self.protocol["server_id"],
                        }
                    ],
                }
            )
        )

    async def call(self, action, payload=None):
        payload = payload or {}
        if action == "plan_identity":
            return {"identity": self.identity}
        if action == "validate_structure":
            atoms = read(io.StringIO(payload["content"]), format=payload["format"])
            stream = io.StringIO()
            write(stream, atoms, format="extxyz")
            return {"extxyz": stream.getvalue(), "structure": payload, "formula": "Cu4", "atoms": 4}
        if action == "resolve_calculation":
            protocol = payload["protocol_snapshot"]
            return {
                "request": {
                    "structure": payload["structure"],
                    "kind": "vasp",
                    "server_id": protocol["server_id"],
                    "code_id": protocol["code_id"],
                    "resources": protocol["resources"],
                    "incar": protocol["incar"],
                    "kmesh": protocol["kmesh"],
                    "potential_family": "PBE",
                    "potential_mapping": {"Cu": "Cu"},
                }
            }
        if action == "preview":
            identifier = str(uuid4())
            self.requests[identifier] = payload
            return {
                "preview_id": identifier,
                "structure": {"formula": "Cu4"},
                "resources": payload["resources"],
                "files": {"POSCAR": payload["structure"]["content"]},
            }
        if action == "submit":
            identifier = payload["preview_id"]
            reused = identifier in self.jobs
            if not reused:
                self.jobs[identifier] = {
                    "id": str(uuid4()),
                    "pk": len(self.jobs) + 1,
                    "process_state": "waiting",
                    "finished_ok": False,
                }
                self.submits.append(identifier)
            return {"job": dict(self.jobs[identifier]), "reused": reused}
        if action == "job_details":
            if self.unavailable:
                raise AgentError("automation_timeout", "测试连接中断", 504)
            return dict(next(job for job in self.jobs.values() if job["id"] == payload["job_id"]))
        if action == "job_structure":
            validated = await self.call(
                "validate_structure", {"content": structure_text(3.75), "format": "vasp"}
            )
            return {
                **validated,
                "source_job_id": payload["job_id"],
                "source_job_pk": 1,
                "source_structure_uuid": payload["job_id"],
            }
        if action == "stage_files":
            from pathlib import Path

            directory = Path(payload["directory"])
            directory.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
            for name in payload["files"]:
                (directory / name).write_text("retrieved frequencies")
            return {"files": {name: str(directory / name) for name in payload["files"]}}
        raise AssertionError(action)

    def finish(self, preview, *, converged=True, success=True):
        self.jobs[preview].update(
            process_state="finished",
            finished_ok=success,
            exit_status=0 if success else 700,
            output_structure={"uuid": str(uuid4())},
            results={
                "run_status": {"electronic_converged": converged, "ionic_converged": converged},
                "total_energies": {"energy_extrapolated": -10.5},
            },
        )


@pytest.fixture
async def graph(settings_factory):
    engine = Engine(settings_factory(automation_enabled=True))
    chat = CalculationChat(engine)
    context = ToolContext(str(uuid4()), str(uuid4()))
    initial = await chat.import_structure(
        ImportInput(
            name="initial", structure=StructureInput(format="vasp", content=structure_text())
        ),
        context,
    )
    return chat, context, engine, initial["id"]


def vasp(identifier, ref, **kwargs):
    return {
        "id": identifier,
        "title": identifier,
        "kind": "vasp",
        "inputs": {"structure": ref},
        "calculation": {"protocol_id": "pbe", "require_ionic": True, **kwargs},
    }


def draft(nodes, **kwargs):
    return PlanDraft(title="研究方案", goal="可执行依赖关系", nodes=nodes, **kwargs)


def test_graph_validation_and_auto_dependencies():
    root = {"id": "model", "title": "建模", "kind": "model", "code": "pass"}
    model = draft([root, vasp("relax", {"node": "model"})])
    assert model.nodes[1].depends_on == ["model"]
    for nodes in [
        [root, root],
        [vasp("a", {"node": "b"}), vasp("b", {"node": "a"})],
        [vasp("a", {"node": "absent"})],
        [{**root, "inputs": {"../secret": {"structure_id": str(uuid4())}}}],
        [{**root, "code": "def invalid("}],
        [
            {"id": "analysis", "title": "分析", "kind": "analysis", "code": "pass"},
            vasp("a", {"node": "analysis"}),
        ],
    ]:
        with pytest.raises(ValidationError):
            draft(nodes)


async def test_review_revisions_and_no_unapproved_execution(graph):
    chat, context, engine, structure = graph
    plan = await chat.plans.draft(draft([vasp("a", {"structure_id": structure})]), context)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert not engine.submits
    new = await chat.plans.draft(
        draft(
            [vasp("a", {"structure_id": structure}, incar={"ENCUT": 600})],
            plan_id=plan["id"],
            expected_revision=1,
        ),
        context,
    )
    assert new["revision"] == 2 and new["changed_nodes"] == ["a"]
    with pytest.raises(AgentError, match="方案已更新"):
        await chat.plans.confirm(context.session_id, plan["id"], 1)
    await chat.plans.confirm(context.session_id, plan["id"], 2)
    with pytest.raises(AgentError, match="方案已确认"):
        await chat.plans.draft(
            draft(
                [vasp("a", {"structure_id": structure})], plan_id=plan["id"], expected_revision=2
            ),
            context,
        )
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 1
    assert engine.requests[engine.submits[0]]["incar"]["ENCUT"] == 600


async def test_actual_relaxed_structure_parallel_branches_and_restart(graph, monkeypatch):
    chat, context, engine, structure = graph
    observed = []

    async def execute(directory, code, *, output_format):
        inputs = json.loads((directory / "inputs.json").read_text())
        if output_format == "vasp":
            atoms = read(directory / "bulk.vasp")
            observed.append(atoms.cell.lengths()[0])
            return structure_text(atoms.cell.lengths()[0])
        assert inputs["left"]["result"]["total_energies"]["energy_extrapolated"] == -10.5
        assert inputs["right"]["result"]["total_energies"]["energy_extrapolated"] == -10.5
        assert (
            directory / inputs["left"]["files"]["OUTCAR"]
        ).read_text() == "retrieved frequencies"
        return '{"adsorption_energy_ev": -1.2}'

    monkeypatch.setattr("chemistry_agent.automation.plans.execute_model", execute)
    nodes = [
        vasp("bulk", {"structure_id": structure}),
        {
            "id": "slab",
            "title": "切面",
            "kind": "model",
            "code": "pass",
            "inputs": {"bulk": {"node": "bulk"}},
        },
        vasp("left", {"node": "slab"}),
        vasp("right", {"node": "slab"}),
        {
            "id": "summary",
            "title": "汇总",
            "kind": "analysis",
            "code": "pass",
            "result_files": ["OUTCAR"],
            "inputs": {
                "left": {"node": "left", "output": "result"},
                "right": {"node": "right", "output": "result"},
            },
        },
    ]
    plan = await chat.plans.draft(draft(nodes), context)
    await chat.plans.confirm(context.session_id, plan["id"], 1)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 1 and not observed
    engine.finish(engine.submits[0])
    # A new executor loads the persisted graph without submitting the bulk twice.
    chat.plans = CalculationPlans(chat)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert observed == [3.75]  # Actual optimized lattice, not the initial 3.6.
    assert len(engine.submits) == 3
    assert (
        chat.load(context.session_id, "plans", plan["id"])["runtime"]["summary"]["status"]
        == "pending"
    )
    for preview in engine.submits[1:]:
        engine.finish(preview)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    done = chat.load(context.session_id, "plans", plan["id"])
    assert done["status"] == "completed"
    assert done["runtime"]["summary"]["outputs"]["result"]["adsorption_energy_ev"] == -1.2
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 3


async def test_concurrency_pause_failure_and_transport_outage(graph):
    chat, context, engine, structure = graph
    plan = await chat.plans.draft(
        draft(
            [
                vasp("a", {"structure_id": structure}),
                vasp("b", {"structure_id": structure}),
                vasp("dependent", {"node": "a"}),
            ],
            max_parallel=1,
        ),
        context,
    )
    await chat.plans.confirm(context.session_id, plan["id"], 1)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 1
    engine.unavailable = True
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert chat.load(context.session_id, "plans", plan["id"])["runtime"]["a"]["status"] == "running"
    engine.unavailable = False
    await chat.plans.control(context.session_id, plan["id"], "pause")
    engine.finish(engine.submits[0], converged=False)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 1
    await chat.plans.control(context.session_id, plan["id"], "resume")
    await chat.plans.tick_plan(context.session_id, plan["id"])
    saved = chat.load(context.session_id, "plans", plan["id"])
    assert saved["runtime"]["dependent"]["status"] == "blocked"
    assert len(engine.submits) == 2  # Independent branch still proceeds.
    engine.finish(engine.submits[1])
    await chat.plans.tick_plan(context.session_id, plan["id"])
    assert chat.load(context.session_id, "plans", plan["id"])["status"] == "failed"


async def test_submission_checkpoint_prevents_duplicate_after_disconnect(graph, monkeypatch):
    chat, context, engine, structure = graph
    plan = await chat.plans.draft(draft([vasp("a", {"structure_id": structure})]), context)
    await chat.plans.confirm(context.session_id, plan["id"], 1)
    original = chat.submit_reviewed

    async def interrupted(session, preview):
        await engine.call("submit", {"preview_id": preview})
        raise asyncio.CancelledError()

    import asyncio

    monkeypatch.setattr(chat, "submit_reviewed", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await chat.plans.tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 1
    monkeypatch.setattr(chat, "submit_reviewed", original)
    await CalculationPlans(chat).tick_plan(context.session_id, plan["id"])
    assert len(engine.submits) == 1
    assert chat.load(context.session_id, "plans", plan["id"])["runtime"]["a"]["status"] == "running"


async def test_frozen_protocol_and_environment_drift(graph):
    chat, context, engine, structure = graph
    plan = await chat.plans.draft(draft([vasp("a", {"structure_id": structure})]), context)
    engine.protocol["incar"]["ENCUT"] = 999
    await chat.plans.confirm(context.session_id, plan["id"], 1)
    engine.identity = "changed"
    await chat.plans.tick_plan(context.session_id, plan["id"])
    saved = chat.load(context.session_id, "plans", plan["id"])
    assert saved["nodes"][0]["effective"]["incar"]["ENCUT"] == 520
    assert saved["runtime"]["a"]["error_code"] == "plan_environment_changed"
    assert not engine.submits


async def test_analysis_condition_blocks_downstream(graph, monkeypatch):
    chat, context, engine, structure = graph

    async def failed_gate(*args, **kwargs):
        return '{"passed":false,"reason":"发现虚频"}'

    monkeypatch.setattr("chemistry_agent.automation.plans.execute_model", failed_gate)
    plan = await chat.plans.draft(
        draft(
            [
                {"id": "gate", "title": "虚频检查", "kind": "analysis", "code": "pass"},
                {**vasp("a", {"structure_id": structure}), "depends_on": ["gate"]},
            ]
        ),
        context,
    )
    await chat.plans.confirm(context.session_id, plan["id"], 1)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    saved = chat.load(context.session_id, "plans", plan["id"])
    assert saved["runtime"]["gate"]["error"] == "发现虚频"
    assert saved["runtime"]["a"]["status"] == "blocked"
    assert not engine.submits


def test_agent_can_create_and_revise_executable_plan_through_conversation(settings_factory):
    import httpx
    from fastapi.testclient import TestClient

    from chemistry_agent.api import create_app

    settings = settings_factory(automation_enabled=True)
    engine = Engine(settings)
    saved = {}
    phase = 0

    def completion(request):
        nonlocal phase
        payload = json.loads(request.content)
        phase += 1
        if phase in {1, 3}:
            nodes = [{"id": "build", "title": "建立惯用胞", "kind": "model", "code": "pass"}]
            if phase == 1:
                nodes.append(
                    {
                        "id": "inspect",
                        "title": "分析",
                        "kind": "analysis",
                        "code": "pass",
                        "inputs": {"cell": {"node": "build", "output": "result"}},
                    }
                )
            args = {"title": "共同设计", "goal": "先建模再分析", "nodes": nodes}
            if saved:
                args.update(plan_id=saved["id"], expected_revision=saved["revision"])
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(uuid4()),
                        "type": "function",
                        "function": {
                            "name": "calculation_plan_save",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }
        else:
            result = json.loads(payload["messages"][-1]["content"])
            saved.update(result)
            message = {"role": "assistant", "content": "方案已更新，请在右侧查看确认。"}
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}]}
        )

    app = create_app(settings, transport=httpx.MockTransport(completion))
    app.state.automation.call = engine.call
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as client:
        session = client.post("/sessions", json={"title": "设计"}).json()["id"]
        base = f"/api/automation/chat/{session}/workflows"
        response = client.post(
            "/chat", json={"session_id": session, "message": "设计建模和分析流程"}
        )
        assert response.status_code == 200, response.text
        first = client.get(base).json()["plans"][0]
        assert len(first["nodes"]) == 2 and first["status"] == "draft"
        response = client.post("/chat", json={"session_id": session, "message": "先去掉分析"})
        assert response.status_code == 200, response.text
        second = client.get(base).json()["plans"][0]
        assert second["revision"] == 2 and second["removed_nodes"] == ["inspect"]
        assert not engine.submits
        assert client.post(f"{base}/{first['id']}/confirm", json={"revision": 1}).status_code == 409
        other = client.post("/sessions", json={"title": "other"}).json()["id"]
        assert (
            client.post(
                f"/api/automation/chat/{other}/workflows/{first['id']}/confirm",
                json={"revision": 2},
            ).status_code
            == 404
        )
        assert client.post(f"{base}/{first['id']}/confirm", json={"revision": 2}).status_code == 200


def test_analysis_download_manifest_excludes_inputs_and_links(tmp_path):
    from chemistry_agent.automation.plans import generated_files

    (tmp_path / "inputs.json").write_text("{}")
    (tmp_path / "model.py").write_text("pass")
    (tmp_path / "source.vasp").write_text("input")
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "large.raw").write_bytes(b"raw input")
    (tmp_path / "result.json").write_text("{}")
    (tmp_path / "plot.svg").write_text("<svg/>")
    (tmp_path / "link").symlink_to("/etc/passwd")
    assert generated_files(tmp_path, {"source"}) == ["plot.svg", "result.json"]


async def test_model_can_use_structure_as_input_alias(graph, monkeypatch):
    chat, context, engine, structure = graph
    plan = await chat.plans.draft(
        draft(
            [
                {
                    "id": "modify",
                    "title": "修改结构",
                    "kind": "model",
                    "code": "pass",
                    "inputs": {"structure": {"structure_id": structure}},
                }
            ]
        ),
        context,
    )

    async def execute(directory, code, *, output_format):
        atoms = read(directory / "structure.vasp")
        atoms.set_cell(atoms.cell * 1.1, scale_atoms=True)
        write(directory / "structure.vasp", atoms, format="vasp")
        return (directory / "structure.vasp").read_text()

    monkeypatch.setattr("chemistry_agent.automation.plans.execute_model", execute)
    await chat.plans.confirm(context.session_id, plan["id"], 1)
    await chat.plans.tick_plan(context.session_id, plan["id"])
    result = chat.load(context.session_id, "plans", plan["id"])
    assert result["status"] == "completed"
    saved = chat.load(
        context.session_id, "structures", result["runtime"]["modify"]["outputs"]["structure_id"]
    )
    assert read(io.StringIO(saved["extxyz"]), format="extxyz").cell.lengths()[0] == pytest.approx(
        3.96
    )


async def test_auto_large_plan_is_idempotent_and_queue_scoped(graph):
    from chemistry_agent.automation.plan_authority import authorized_queue, auto_queue

    chat, context, engine, structure = graph
    assert authorized_queue("/auto low\n直接运行") == "low"
    assert authorized_queue("解释一下 /auto low") is None
    nodes = [vasp(f"site_{i}", {"structure_id": structure}) for i in range(105)]
    spec = draft(nodes)
    token = auto_queue.set("low")
    try:
        with pytest.raises(AgentError, match="low"):
            await chat.plans.draft(spec, context)
        auto_queue.set("batch")
        plan = await chat.plans.draft(spec, context)
        again = await chat.plans.draft(spec, context)
        assert plan["status"] == "running" and plan["max_parallel"] is None
        assert again["id"] == plan["id"]
        await chat.plans.tick_plan(context.session_id, plan["id"])
        assert len(set(engine.submits)) == 105
    finally:
        auto_queue.reset(token)


async def test_repair_preserves_completed_sibling_and_reopens_descendants(graph):
    from chemistry_agent.automation.plan_authority import auto_queue, repair_target
    from chemistry_agent.automation.plan_models import NodeRepair

    chat, ctx, engine, structure = graph
    ref = {"structure_id": structure}
    spec = draft([vasp("good", ref), vasp("bad", ref), vasp("child", {"node": "bad"})])
    grant = auto_queue.set("batch")
    try:
        p = await chat.plans.draft(spec, ctx)
    finally:
        auto_queue.reset(grant)
    await chat.plans.tick_plan(ctx.session_id, p["id"])
    good, bad = engine.submits
    engine.finish(good)
    engine.finish(bad, success=False)
    await chat.plans.tick_plan(ctx.session_id, p["id"])
    args = NodeRepair(plan_id=p["id"], node=vasp("bad", ref, incar={"NELM": 100}), reason="SCF")
    with pytest.raises(AgentError, match="故障处理"):
        await chat.plans.repair_node(args, ctx)
    grant = repair_target.set((ctx.session_id, p["id"]))
    try:
        await chat.plans.repair_node(args, ctx)
        with pytest.raises(AgentError):
            await chat.plans.repair_node(args, ctx)
    finally:
        repair_target.reset(grant)
    await chat.plans.tick_plan(ctx.session_id, p["id"])
    fixed = chat.load(ctx.session_id, "plans", p["id"])
    assert len(engine.submits) == 3 and fixed["runtime"]["good"]["status"] == "completed"
    assert fixed["runtime"]["bad"]["attempts"][0]["state"]["preview_id"] == bad
    assert fixed["runtime"]["child"]["status"] == "pending"
