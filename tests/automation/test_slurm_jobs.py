"""Real shell/files with a local Slurm fixture; no SSH, daemon, model quota or cluster."""

import asyncio
import json
import signal
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chemistry_agent.automation import slurm_remote
from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.job_events import job_event_target, record_events, wake
from chemistry_agent.automation.job_models import FileReference
from chemistry_agent.automation.plan_authority import auto_queue, repair_target
from chemistry_agent.automation.plan_models import NodeRepair, PlanDraft, PlanExtension
from chemistry_agent.automation.slurm_jobs import SlurmJobs
from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import TokenUsage
from chemistry_agent.store import Store
from chemistry_agent.tools import ToolContext


class NoAiida:
    def __init__(self, settings):
        self.settings = settings

    async def call(self, *args, **kwargs):
        raise AssertionError("Generic jobs must not call AiiDA")


class LocalSlurm(SlurmJobs):
    def hosts(self):
        return ["test"]

    async def request(self, host, payload):
        assert host == "test"
        return await asyncio.to_thread(slurm_remote.dispatch, payload)


@pytest.fixture
async def cluster(tmp_path, monkeypatch, settings_factory):
    binary = tmp_path / "bin"
    binary.mkdir()
    script = f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ['FAKE_SLURM'])
state = root / 'scheduler.json'
rows = json.loads(state.read_text()) if state.exists() else {{}}
name = Path(sys.argv[0]).name
if name == 'sbatch':
    if (root / 'reject').exists():
        print('sbatch: error: QOSMaxSubmitJobPerUserLimit', file=sys.stderr)
        sys.exit(1)
    key = str(100 + len(rows))
    folder = Path(sys.argv[sys.argv.index('--chdir')+1])
    text = (folder/'_chem_submit.sh').read_text()
    label = next(x.split('=',1)[1] for x in text.splitlines() if '--job-name=' in x)
    with (folder/'_chem_stdout.txt').open('w') as out, (folder/'_chem_stderr.txt').open('w') as err:
        proc = subprocess.run(['bash',str(folder/'_chem_submit.sh')],
                              cwd=folder,stdout=out,stderr=err)
    rows[key] = {{'name':label,'state':'COMPLETED' if proc.returncode == 0 else 'FAILED',
                 'exit':f'{{proc.returncode}}:0'}}
    state.write_text(json.dumps(rows))
    print(key)
elif name == 'squeue':
    if '-n' in sys.argv:
        wanted = sys.argv[sys.argv.index('-n')+1]
        for key,row in rows.items():
            if row['name']==wanted and row['state']=='RUNNING': print(key+'|'+wanted)
elif name == 'sacct':
    if (root/'accounting_down').exists(): sys.exit(1)
    if '--name' in sys.argv:
        wanted=sys.argv[sys.argv.index('--name')+1]
        for key,row in rows.items():
            if row['name']==wanted: print(key+'|'+wanted+'|')
    else:
        key=sys.argv[sys.argv.index('-j')+1]
        if key in rows:
            row=rows[key]; print(key+'|'+row['state']+'|'+row['exit']+'|')
elif name == 'scancel':
    rows[sys.argv[1]]['state']='CANCELLED'; state.write_text(json.dumps(rows))
"""
    for command in ("sbatch", "squeue", "sacct", "scancel"):
        path = binary / command
        path.write_text(script)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_SLURM", str(tmp_path))
    settings = settings_factory(automation_enabled=True)
    chat = CalculationChat(NoAiida(settings))
    chat.plans.slurm = LocalSlurm(chat)
    context = ToolContext(str(uuid4()), str(uuid4()))
    return chat.plans, context, tmp_path


def job(tmp_path, key="compute", script="printf '2' > result.dat", **extra):
    return {
        "id": key,
        "title": key,
        "kind": "job",
        "job": {
            "host": "test",
            "workdir": str(tmp_path / "remote"),
            "script": script,
            "resources": {"queue": "low", "wallclock_seconds": 60},
            "retrieve": ["result.dat"],
            "required_outputs": ["result.dat"],
            **extra,
        },
    }


async def draft(plans, context, nodes):
    result = await plans.draft(
        PlanDraft(title="test", goal="software-neutral test", nodes=nodes), context
    )
    await plans.confirm(context.session_id, result["id"], result["revision"])
    return result["id"]


async def tick(plans, context, identifier):
    plan = plans.chat.load(context.session_id, "plans", identifier)
    for state in plan["runtime"].values():
        state.pop("polled_at", None)
    plans.save(context.session_id, plan)
    await plans.tick_plan(context.session_id, identifier)
    return plans.chat.load(context.session_id, "plans", identifier)


async def test_script_to_binary_dependency_and_restart(cluster):
    plans, context, tmp = cluster
    producer = job(tmp, "first", "printf '\\000\\001hello' > result.dat")
    consumer = job(tmp, "second", "cp input/data.bin result.dat")
    consumer["files"] = [{"node": "first", "path": "result.dat", "target": "input/data.bin"}]
    identifier = await draft(plans, context, [producer, consumer])
    initial = await tick(plans, context, identifier)
    assert initial["nodes"][1]["depends_on"] == ["first"]
    assert initial["runtime"]["second"]["status"] == "pending"
    # Recreate task manager as if the application restarted.
    plans.slurm = LocalSlurm(plans.chat)
    await tick(plans, context, identifier)
    completed = await tick(plans, context, identifier)
    assert completed["status"] == "completed"
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
    for state in completed["runtime"].values():
        assert state["job"]["scheduler_job_id"]
        folder = plans.chat.directory(context.session_id, "plan_runs") / identifier
        name = next(n for n in state["outputs"]["files"] if n.endswith("/result.dat"))
        node = next(k for k, v in completed["runtime"].items() if v == state)
        assert (folder / node / name).read_bytes() == b"\x00\x01hello"
        if node == "second":
            manifest = json.loads(
                (
                    folder / node / "_job_inputs" / state["generic_job_id"] / "file_inputs.json"
                ).read_text()
            )
            assert manifest["input/data.bin"]["transfer"] == "remote_copy"
    await tick(plans, context, identifier)
    assert len(completed["job_events"]) == 2
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 2


async def test_two_environments_in_one_plan_file_handoff_and_restart(cluster):
    """Two actual Runner dispatches share the platform session, not a remote directory."""
    from chemistry_agent.environments.registry import Environment, Environments

    plans, context, tmp = cluster
    environments = Environments(plans.chat.service.settings.platform_path)
    for host in ("cpu", "gpu"):
        environments.save(Environment(id=host, name=host, transport="local"))
    plans.slurm = SlurmJobs(plans.chat)
    producer = job(tmp, "reference", "printf '\\000\\001training-data' > result.dat")
    producer["job"].update(host="cpu", workdir=str(tmp / "cpu"))
    consumer = job(tmp, "training", "cp dataset.bin result.dat")
    consumer["job"].update(host="gpu", workdir=str(tmp / "gpu"))
    consumer["files"] = [{"node": "reference", "path": "result.dat", "target": "dataset.bin"}]
    identifier = await draft(plans, context, [producer, consumer])
    await tick(plans, context, identifier)
    plans.slurm = SlurmJobs(plans.chat)
    await tick(plans, context, identifier)
    done = await tick(plans, context, identifier)
    assert done["status"] == "completed"
    state = done["runtime"]["training"]
    directory = plans.chat.directory(context.session_id, "plan_runs") / identifier
    manifest = json.loads((directory / "training" / "_job_inputs" /
                           state["generic_job_id"] / "file_inputs.json").read_text())
    assert len(manifest["dataset.bin"]["sha256"]) == 64
    assert manifest["dataset.bin"]["node"] == "reference"
    await tick(plans, context, identifier)
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 2


async def test_failure_preserves_files_blocks_dependents_and_agent_repairs(cluster):
    plans, context, tmp = cluster
    first = job(tmp, script="echo partial > result.dat; exit 7")
    second = job(tmp, "after")
    second["depends_on"] = ["compute"]
    token = auto_queue.set("low")
    try:
        plan = await plans.draft(
            PlanDraft(title="repair", goal="repair", nodes=[first, second]), context
        )
    finally:
        auto_queue.reset(token)
    await tick(plans, context, plan["id"])
    failed = await tick(plans, context, plan["id"])
    assert failed["runtime"]["compute"]["status"] == "failed"
    assert failed["runtime"]["after"]["status"] == "blocked"
    assert failed["runtime"]["compute"]["outputs"]["files"]
    first["job"]["script"] = "printf 'repaired' > result.dat"
    token = repair_target.set((context.session_id, plan["id"]))
    try:
        await plans.repair_node(
            NodeRepair(plan_id=plan["id"], node=first, reason="fix script"), context
        )
    finally:
        repair_target.reset(token)
    await tick(plans, context, plan["id"])
    await tick(plans, context, plan["id"])
    done = await tick(plans, context, plan["id"])
    assert done["status"] == "completed"
    assert len(done["runtime"]["compute"]["attempts"]) == 1


@pytest.mark.parametrize("partial_output", [False, True])
async def test_accounting_outage_never_means_success(cluster, monkeypatch, partial_output):
    plans, context, tmp = cluster
    identifier = await draft(plans, context, [job(tmp)])
    await tick(plans, context, identifier)
    if partial_output:
        original = slurm_remote.run

        def run(arguments):
            result = original(arguments)
            if arguments[0] == "sacct":
                return subprocess.CompletedProcess(arguments, 1, result.stdout, "partial result")
            return result

        monkeypatch.setattr(slurm_remote, "run", run)
    else:
        (tmp / "accounting_down").touch()
    waiting = await tick(plans, context, identifier)
    assert waiting["status"] == "running"
    assert waiting["runtime"]["compute"]["job"]["scheduler_state"] == "unknown"
    assert not waiting["job_events"]
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 1


async def test_submit_response_lost_reconciles_without_resubmission(cluster, monkeypatch):
    plans, context, tmp = cluster
    original = plans.slurm.request
    lost = False

    async def request(host, payload):
        nonlocal lost
        result = await original(host, payload)
        if payload["action"] == "submit" and not lost:
            lost = True
            raise AgentError("timeout", "response lost", 503)
        return result

    monkeypatch.setattr(plans.slurm, "request", request)
    identifier = await draft(plans, context, [job(tmp)])
    plan = await tick(plans, context, identifier)
    assert plan["runtime"]["compute"]["status"] == "preparing"
    await tick(plans, context, identifier)
    plan = await tick(plans, context, identifier)
    assert plan["status"] == "completed"
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 1


@pytest.mark.parametrize("accounting_fault", ["partial_error", "empty", "ambiguous"])
async def test_remote_submit_uncertain_until_unambiguous_accounting(
    cluster, monkeypatch, accounting_fault
):
    plans, context, tmp = cluster
    original = slurm_remote.run
    fault = accounting_fault
    submissions = 0

    def run(arguments):
        nonlocal submissions
        result = original(arguments)
        if arguments[0] == "sbatch":
            submissions += 1
            # The scheduler accepted the job, but the remote helper never got its response.
            raise subprocess.TimeoutExpired(arguments[0], 25)
        if arguments[0] == "sacct" and "--name" in arguments and fault:
            if fault == "partial_error":
                return subprocess.CompletedProcess(
                    arguments, 1, result.stdout, "accounting unavailable"
                )
            if fault == "empty":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            name = arguments[arguments.index("--name") + 1]
            return subprocess.CompletedProcess(arguments, 0, result.stdout + f"101|{name}|\n", "")
        return result

    monkeypatch.setattr(slurm_remote, "run", run)
    identifier = await draft(plans, context, [job(tmp)])
    with pytest.raises(subprocess.TimeoutExpired):
        await tick(plans, context, identifier)
    plans.slurm = LocalSlurm(plans.chat)
    for _ in range(2):
        waiting = await tick(plans, context, identifier)
        assert "scheduler_job_id" not in waiting["runtime"]["compute"].get("job", {})
        assert not waiting["job_events"]
        assert submissions == 1
    fault = None
    for _ in range(3):
        plan = await tick(plans, context, identifier)
    assert plan["status"] == "completed"
    assert submissions == 1
    assert set(json.loads((tmp / "scheduler.json").read_text())) == {
        plan["runtime"]["compute"]["job"]["scheduler_job_id"]
    }


async def test_submit_helper_recovers_after_process_is_killed(cluster):
    _, _, tmp = cluster
    root = tmp / "isolated-job"
    root.mkdir()
    (root / "_chem_submit.sh").write_text(
        "#!/bin/bash\n#SBATCH --job-name=chem-crash-test\nprintf done > result.dat\n"
    )
    helper = tmp / "interrupt_submit.py"
    helper.write_text(
        "from threading import Event\n"
        "from chemistry_agent.automation import slurm_remote\n\n"
        "original_run = slurm_remote.run\n\n"
        "def interrupt_after_acceptance(arguments: list[str]):\n"
        "    result = original_run(arguments)\n"
        "    if arguments[0] == 'sbatch' and result.returncode == 0:\n"
        "        print('accepted', flush=True)\n"
        "        Event().wait()\n"
        "    return result\n\n"
        "if __name__ == '__main__':\n"
        "    slurm_remote.run = interrupt_after_acceptance\n"
        "    slurm_remote.main()\n"
    )
    request = {"action": "submit", "root": str(root), "id": "crash-test", "digest": "test"}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", str(helper), stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        process.stdin.write(json.dumps(request).encode())
        await process.stdin.drain()
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"accepted\n"
        assert json.loads((root / "_chem_receipt.json").read_text())["status"] == "uncertain"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    assert process.returncode == -signal.SIGKILL

    recovered = await asyncio.to_thread(slurm_remote.dispatch, request)
    replayed = await asyncio.to_thread(slurm_remote.dispatch, request)
    assert recovered == replayed
    assert recovered["status"] == "submitted"
    assert set(json.loads((tmp / "scheduler.json").read_text())) == {recovered["scheduler_job_id"]}


async def test_quota_wait_release_consumed_once(cluster):
    plans, context, tmp = cluster
    identifier = await draft(plans, context, [job(tmp, "accepted")])
    await tick(plans, context, identifier)
    (tmp / "reject").touch()
    waiting_id = await draft(plans, context, [job(tmp, "wait_one"), job(tmp, "wait_two")])
    await tick(plans, context, waiting_id)
    await tick(plans, context, waiting_id)
    (tmp / "reject").unlink()
    await tick(plans, context, waiting_id)
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 1
    await tick(plans, context, identifier)  # One accepted-job completion event.
    waiting = await tick(plans, context, waiting_id)
    assert len(json.loads((tmp / "scheduler.json").read_text())) == 2
    assert (
        sum(s["job"].get("scheduler_job_id") is not None for s in waiting["runtime"].values()) == 1
    )


async def test_generic_job_extension_is_idempotent(cluster):
    plans, context, tmp = cluster
    token = auto_queue.set("low")
    try:
        plan = await plans.draft(
            PlanDraft(title="dynamic", goal="dynamic", nodes=[job(tmp)]), context
        )
    finally:
        auto_queue.reset(token)
    next_node = job(tmp, "next")
    next_node["files"] = [{"node": "compute", "path": "result.dat", "target": "input.bin"}]
    request = PlanExtension(plan_id=plan["id"], nodes=[next_node], reason="actual output available")
    token = job_event_target.set((context.session_id, plan["id"]))
    try:
        once = await plans.extend(request, context)
        twice = await plans.extend(request, context)
    finally:
        job_event_target.reset(token)
    assert once["revision"] == twice["revision"]
    assert twice["added_nodes"] == ["next"]
    assert len(plans.chat.load(context.session_id, "plans", plan["id"])["nodes"]) == 2


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "archive/file", "_chem_exit_code"])
def test_dependency_paths(path):
    with pytest.raises(ValidationError):
        FileReference(node="source", path="data", target=path)


def test_file_dependencies_detect_cycles(tmp_path):
    a, b = job(tmp_path, "a"), job(tmp_path, "b")
    a["files"] = [{"node": "b", "path": "data", "target": "data"}]
    b["files"] = [{"node": "a", "path": "data", "target": "data"}]
    with pytest.raises(ValidationError, match="环"):
        PlanDraft(title="cycle", goal="cycle", nodes=[a, b])


async def test_event_wakes_tool_agent_once_without_visible_prompt(cluster):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "events.sqlite3")
    session = await store.create_session("event")
    context = ToolContext(session["id"], context.run_id)
    plans.store = store

    class Provider:
        def configuration(self):
            from types import SimpleNamespace

            return None, SimpleNamespace(provider="deepseek", base_url="https://test")

        def select_model(self, model):
            pass

    class Agent:
        calls = 0

        async def run(self, messages, progress, **kwargs):
            self.calls += 1
            assert "后台任务结束事件" in messages[-1]["content"]
            # Exercise the same inspection tool available to the application agent.
            from chemistry_agent.automation.job_tools import JobFileQuery, tools

            evidence = json.loads(messages[-1]["content"].split("\n", 1)[1])
            identifier = evidence["events"][0]["job_id"]
            tool = tools(plans)[0]
            result = await tool.handler(
                JobFileQuery(job_id=identifier, mode="read", path="result.dat"), context
            )
            assert result["content"] == "2"
            return {
                "messages": [*messages, {"role": "assistant", "content": "已读取结果 2"}],
                "tool_calls": [],
                "usage": TokenUsage(),
            }

    identifier = await draft(plans, context, [job(tmp)])
    await tick(plans, context, identifier)
    plan = await tick(plans, context, identifier)
    record_events(plan)
    plans.save(context.session_id, plan)
    plans.provider, plans.agent = Provider(), Agent()
    try:
        await wake(plans, context.session_id, identifier)
        await wake(plans, context.session_id, identifier)
        assert plans.agent.calls == 1
        session = await store.get_session(context.session_id)
        assert all("后台任务结束事件" not in str(m) for m in session["messages"])
        assert any("已读取结果 2" in str(m) for m in session["messages"])
    finally:
        await store.close()


def test_array_accounting_checks_every_task(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    (tmp_path / "_chem_receipt.json").write_text(
        json.dumps({"status": "submitted", "scheduler_job_id": "500"})
    )
    (tmp_path / "_chem_exit_code_0").write_text("0")
    (tmp_path / "_chem_exit_code_1").write_text("9")

    def run(args):
        output = (
            ""
            if args[0] == "squeue"
            else ("500_0|COMPLETED|0:0|\n500_0.batch|COMPLETED|0:0|\n500_1|FAILED|9:0|\n")
        )
        return CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(slurm_remote, "run", run)
    result = slurm_remote.status(tmp_path)
    assert result["terminal"] and not result["finished_ok"]
    assert len(result["tasks"]) == 2
    assert result["exit_code"] == "9:0"


async def test_event_stays_pending_while_user_is_talking(cluster):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "busy.sqlite3")
    session = await store.create_session("busy")
    context = ToolContext(session["id"], context.run_id)
    plans.store = store
    identifier = await draft(plans, context, [job(tmp)])
    await tick(plans, context, identifier)
    await tick(plans, context, identifier)
    run_id, _ = await store.begin_run(context.session_id)
    try:
        await wake(plans, context.session_id, identifier)
        plan = plans.chat.load(context.session_id, "plans", identifier)
        assert next(iter(plan["job_events"].values()))["status"] == "pending"
    finally:
        await store.fail_run(
            context.session_id, run_id, "test-ended", tool_calls=[], usage=TokenUsage()
        )
        await store.close()


async def test_job_tool_context_and_cross_session_isolation(cluster):
    from chemistry_agent.automation.job_tools import tools
    from chemistry_agent.provider import ToolCall
    from chemistry_agent.tools import ToolRegistry

    plans, context, tmp = cluster
    identifier = await draft(plans, context, [job(tmp)])
    plan = await tick(plans, context, identifier)
    job_id = plan["runtime"]["compute"]["generic_job_id"]
    registry = ToolRegistry(tools(plans))
    call = ToolCall(
        id="read",
        type="function",
        function={
            "name": "calculation_job_files",
            "arguments": json.dumps({"job_id": job_id, "mode": "status"}),
        },
    )
    result = await registry.execute(call, context)
    assert result.success
    other = await registry.execute(call, ToolContext(str(uuid4()), str(uuid4())))
    assert not other.success and other.result["error"] == "artifact_not_found"


async def test_real_agent_loop_reads_output_then_extends_workflow(cluster):
    import httpx

    from chemistry_agent.api import create_app

    original, _, tmp = cluster
    phase = 0
    saved = {}

    def completion(request):
        nonlocal phase
        phase += 1
        data = json.loads(request.content)
        if phase == 1:
            event = json.loads(data["messages"][-1]["content"].split("\n", 1)[1])
            saved.update(event)
            name, arguments = (
                "calculation_job_files",
                {
                    "job_id": event["events"][0]["job_id"],
                    "mode": "read",
                    "path": "result.dat",
                },
            )
        elif phase == 2:
            assert json.loads(data["messages"][-1]["content"])["content"] == "2"
            node = job(tmp, "followup", "cat upstream.dat > result.dat")
            node["files"] = [{"node": "compute", "path": "result.dat", "target": "upstream.dat"}]
            name, arguments = (
                "calculation_plan_extend",
                {"plan_id": saved["plan_id"], "nodes": [node], "reason": "output inspected"},
            )
        else:
            result = json.loads(data["messages"][-1]["content"])
            assert result.get("status") == "running" and result["added_nodes"] == ["followup"], (
                result
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "已读结果并追加下一步。"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": str(uuid4()),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    app = create_app(original.chat.service.settings, transport=httpx.MockTransport(completion))
    async with app.router.lifespan_context(app):
        plans = app.state.calculation_chat.plans
        await plans.close()  # Drive monitoring deterministically in this test.
        plans.slurm = LocalSlurm(plans.chat)
        session = await app.state.store.create_session("real tool loop")
        context = ToolContext(session["id"], str(uuid4()))
        token = auto_queue.set("low")
        try:
            plan = await plans.draft(
                PlanDraft(title="agent", goal="continue", nodes=[job(tmp)]), context
            )
        finally:
            auto_queue.reset(token)
        agent = plans.agent
        plans.agent = None
        await tick(plans, context, plan["id"])
        await tick(plans, context, plan["id"])
        plans.agent = agent
        await wake(plans, context.session_id, plan["id"])
        result = plans.chat.load(context.session_id, "plans", plan["id"])
        assert result["runtime"]["followup"]["status"] == "pending"
        assert phase == 3
        plans.agent = None
        await tick(plans, context, plan["id"])
        result = await tick(plans, context, plan["id"])
        assert result["status"] == "completed"
        history = await app.state.store.get_session(context.session_id)
        assert "后台任务结束事件" not in json.dumps(history, ensure_ascii=False)
