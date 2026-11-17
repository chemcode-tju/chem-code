"""Native file events drive publication even when directory discovery is unavailable."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from chemistry_agent.agent import RunProgress
from chemistry_agent.backends.codex import CodexBackend
from chemistry_agent.store import Store
from chemistry_agent.workspace_files import WorkspaceFiles


async def test_native_file_events_publish_scoped_paths_and_deduplicate(tmp_path):
    store = await Store.open(tmp_path / "store.sqlite3")
    try:
        session = (await store.create_session("native files"))["id"]
        run = str(uuid4())
        root = tmp_path / "workspaces"
        folder = root / session
        folder.mkdir(parents=True)
        for name in [
            "new.xyz",
            "modified.txt",
            "renamed.result",
            "existing.txt",
            "failed.txt",
            "deleted.txt",
        ]:
            (folder / name).write_text(name)
        files = WorkspaceFiles(store, root)
        await files.initialize()
        backend = CodexBackend(
            SimpleNamespace(
                workspace_files=files, context=SimpleNamespace(codex_event=AsyncMock())
            ),
            None,
            "",
        )
        rpc = SimpleNamespace(events=asyncio.Queue())

        def event(identifier: str, changes: list[dict], status: str = "completed", **scope) -> dict:
            return {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    **scope,
                    "item": {
                        "id": identifier,
                        "type": "fileChange",
                        "status": status,
                        "changes": changes,
                    },
                },
            }

        changes = [
            {"path": str(folder / "new.xyz"), "kind": {"type": "add"}},
            {"path": "modified.txt", "kind": {"type": "update", "move_path": None}},
            {"path": "old.result", "kind": {"type": "update", "move_path": "renamed.result"}},
            {"path": "deleted.txt", "kind": {"type": "delete"}},
            {"path": "missing.txt", "kind": {"type": "add"}},
        ]
        for message in [
            event(
                "other-turn", [{"path": "existing.txt", "kind": {"type": "add"}}], turnId="other"
            ),
            event(
                "other-thread",
                [{"path": "existing.txt", "kind": {"type": "add"}}],
                threadId="other",
            ),
            event("failed", [{"path": "failed.txt", "kind": {"type": "add"}}], "failed"),
            event("written", changes),
            event("written", changes),
            {"method": "turn/diff/updated", "params": {"diff": "not a file publication event"}},
            {"method": "turn/completed", "params": {"turn": {"id": "turn", "status": "completed"}}},
        ]:
            await rpc.events.put(message)
        progress = RunProgress()
        calls = []
        await backend.consume(rpc, "thread", "turn", session, run, progress, [], calls, [])
        rows = await files.list(session)
        assert {row["path"] for row in rows} == {"new.xyz", "modified.txt", "renamed.result"}
        assert all(row["run_id"] == run for row in rows)
        assert len(progress.tool_calls) == len(calls) == 2
        written = next(call for call in calls if call["call_id"] == "written")
        assert len(written["result"]["file_ids"]) == 3
        assert written["result"]["publication_errors"][0]["path"] == "missing.txt"
        assert written["success"]  # Preview errors never turn a successful edit into a retry.
        for row in rows:
            assert (store.attachment_dir / row["id"]).read_text() == row["path"]
    finally:
        await store.close()
