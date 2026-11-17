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
        settings.platform_path.mkdir(parents=True, exist_ok=True)
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
        (settings.platform_path / "calculation-configuration.json").write_text(
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
    # Legacy VASP lifecycle remains available only by explicit opt-in.
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


async def test_failed_auto_node_wakes_model_with_tools_and_hides_wake_prompt(graph, tmp_path):
    from types import SimpleNamespace

    from chemistry_agent.automation.plan_authority import auto_queue
    from chemistry_agent.automation.plan_models import NodeRepair
    from chemistry_agent.store import Store

    chat, _, engine, _ = graph
    plans = chat.plans
    store = await Store.open(tmp_path / "repair.sqlite3")
    try:
        sid = (await store.create_session("fault recovery"))["id"]
        context = ToolContext(sid, str(uuid4()))
        node = {
            "id": "build",
            "title": "建模",
            "kind": "model",
            "code": "raise ValueError('bad geometry')",
        }
        grant = auto_queue.set("low")
        try:
            plan = await plans.draft(draft([node]), context)
        finally:
            auto_queue.reset(grant)
        state = chat.load(sid, "plans", plan["id"])
        state["runtime"]["build"] = {"status": "failed", "error": "bad geometry"}
        plans.save(sid, state)

        async def respond(messages, progress, *, session_id, run_id, history_count):
            assert session_id == sid and "bad geometry" in messages[-1]["content"]
            await plans.repair_node(
                NodeRepair(
                    plan_id=plan["id"], node={**node, "code": "pass"}, reason="fix geometry builder"
                ),
                ToolContext(sid, run_id),
            )
            return {
                "messages": [{"role": "assistant", "content": "已修复建模代码。"}],
                "tool_calls": [],
                "usage": progress.usage,
            }

        plans.store, plans.agent = store, SimpleNamespace(run=respond)
        plans.provider = SimpleNamespace(
            configuration=lambda: (None, SimpleNamespace(provider="chatgpt", base_url="")),
            select_model=lambda model: None,
        )
        await plans.tick_plan(sid, plan["id"])
        await plans.repair_tasks[sid, plan["id"]]
        fixed = chat.load(sid, "plans", plan["id"])
        assert fixed["status"] == "running" and fixed["runtime"]["build"]["status"] == "pending"
        assert not (await store.get_session(sid))["messages"]
        events = chat.progress.timeline(sid, 0, 100, plan["id"], "build")["events"]
        assert [(e["kind"], e["status"]) for e in events] == [
            ("step", "failed"),
            ("diagnosis", "running"),
            ("repair", "pending"),
            ("interpretation", "completed"),
        ]
        assert events[-1]["message"] == "已修复建模代码。"
    finally:
        await store.close()


def test_vasp_accepts_chemistry_named_structure_port():
    original = vasp("static", {"node": "cu_primitive"})
    original["inputs"]["bulk"] = original["inputs"].pop("structure")
    model = {"id": "cu_primitive", "kind": "model", "title": "Cu", "code": "pass"}
    node = draft([model, original]).nodes[1]
    assert node.inputs["structure"].node == "cu_primitive"
    assert node.depends_on == ["cu_primitive"]
    with pytest.raises(ValidationError):
        draft(
            [
                model,
                {**original, "inputs": {**original["inputs"], "extra": {"node": "cu_primitive"}}},
            ]
        )


@pytest.fixture
async def summary_event_store(tmp_path):
    from chemistry_agent.store import Store

    store = await Store.open(tmp_path / "summary-events.sqlite3")
    store.research_memory_enabled = False
    yield store
    await store.close()


def test_step_summary_expired_claim_recovers_without_duplicate_event(tmp_path, monkeypatch):
    from chemistry_agent.automation.progress import ProgressStore

    path = tmp_path / "events.sqlite3"
    store = ProgressStore(path)
    with store.connect() as db:
        detail = {"attempt": 0, "pk": 7, "job_id": "job", "scheduler_job_id": "123"}
        store.insert(db, "s", "p", "n", "step", "completed", "step", detail, "step:n:0:completed")
        store.queue_summary(db, "s", "p", "step:n:0:completed", {})
    claim = store.claim_summary()
    reopened = ProgressStore(path)
    assert reopened.claim_summary() is None
    monkeypatch.setattr(
        "chemistry_agent.automation.progress.time.time", lambda: claim["lease_until"] + 1
    )
    reclaimed = reopened.claim_summary()
    store.finish_summary(claim, "completed", "stale answer")
    reopened.finish_summary(reclaimed, "completed", "accepted answer")
    reopened.finish_summary(reclaimed, "completed", "duplicated answer")
    assert [r["message"] for r in reopened.timeline("s", 0, 10)["events"]] == [
        "step",
        "accepted answer",
    ]


async def test_group_paths_survive_draft_revision_and_match_single_node_query(graph):
    from chemistry_agent.automation.plan_models import PlanQuery

    chat, context, engine, _ = graph
    seed = {"kind": "model", "id": "opaque", "title": "Seed", "code": "pass"}
    analysis = {
        "kind": "analysis",
        "id": "inspect",
        "title": "Inspect",
        "code": "pass",
        "inputs": {"sample": {"node": "opaque"}},
    }
    first = await chat.plans.draft(
        draft([{**seed, "group_path": ["Method B", "High T"]}, analysis]), context
    )
    key = first["id"]
    revised = await chat.plans.draft(
        draft([seed, analysis], plan_id=key, expected_revision=1), context
    )
    assert not revised["changed_nodes"]
    stored = chat.load(context.session_id, "plans", key)
    assert stored["nodes"][0]["group_path"] == ["Method B", "High T"]
    assert "group_path" not in stored["nodes"][1]
    node = (await chat.plans.query(PlanQuery(plan_id=key, node_id="inspect"), context))["node"]
    assert node == chat.plans.records(context.session_id)[0]["nodes"][1]
    assert node["group_path"] == ["Method B", "High T"]
    cleared = await chat.plans.draft(
        draft([{**seed, "group_path": []}, analysis], plan_id=key, expected_revision=2), context
    )
    assert cleared["changed_nodes"] == ["opaque"]
    assert not cleared["nodes"][1].get("group_path")
    assert not engine.submits
