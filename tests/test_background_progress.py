import json
from uuid import uuid4

from chemistry_agent.automation.wait_ledger import EventCheckpoint
from chemistry_agent.provider import ToolCall
from chemistry_agent.runtime.shell import ShellInput
from chemistry_agent.store import Store
from chemistry_agent.tools import Tool, ToolRegistry


async def test_multiline_tool_argument_preserves_code():
    code = "python3 - <<'PY'\nprint('hello')\nPY"
    observed = []

    async def execute(args):
        observed.append(args.command)
        return {"ok": True}

    registry = ToolRegistry([Tool("capture", "test", ShellInput, execute)])
    raw = json.dumps({"command": code}).replace("\\n", "\n")
    call = ToolCall.model_validate(
        {
            "id": "test",
            "type": "function",
            "function": {
                "name": "capture",
                "arguments": raw,
            },
        }
    )
    result = await registry.execute(call)
    assert result.success and observed == [code]
    call.function.arguments = '{"command":'
    result = await registry.execute(call)
    assert not result.success and "JSON" in result.result["message"]
    assert observed == [code]


async def test_event_checkpoint_publishes_only_public_progress(tmp_path):
    store = await Store.open(tmp_path / "progress.sqlite3")
    try:
        session = (await store.create_session("test"))["id"]
        run, _ = await store.begin_run(session)
        event = str(uuid4())
        await store.db.execute(
            "INSERT INTO agent_events(id,session_id,plan_id,payload,status,created_at,run_id) "
            "VALUES (?,?,?,'{}','running',0,?)",
            (event, session, "plan", run),
        )
        await store.db.commit()
        await EventCheckpoint(store, event).save(
            {
                "tail": [
                    {"role": "system", "content": "hidden wakeup"},
                    {"role": "assistant", "content": "正在生成体相结构。"},
                    {"role": "tool", "content": "raw result"},
                ],
                "calls": [],
                "context": {},
            }
        )
        row = await store.get_run(run)
        assert row["status"] == "running"
        assert row["answer"] == "正在生成体相结构。"
        assert row["context"]["automation_event"] == "job_completion"
        assert (await store.get_session(session))["messages"] == []
    finally:
        await store.close()
