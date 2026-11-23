"""Commit/ACK crash boundaries for the independent monitor's paginated inbox."""

import asyncio
import json
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import pytest
from test_slurm_jobs import cluster as cluster_fixture
from test_slurm_jobs import draft, job

from chemistry_agent.automation.wait_ledger import WaitLedger
from chemistry_agent.platform.runtime import PlatformRuntime
from chemistry_agent.platform.slurm_bridge import Page, SlurmBridge, persist_page
from chemistry_agent.store import Store, utc_now

cluster = cluster_fixture


def page(hub_id: str, *facts: dict) -> Page:
    return Page.model_validate(
        {
            "hub_id": hub_id,
            "claim": uuid4().hex,
            "deliveries": [
                {
                    "id": index,
                    "event": index,
                    "subscription": "subscription",
                    "consumer": "bridge",
                    "queue": "cluster-a",
                    "payload": {
                        "job_id": str(index),
                        "submitted": "2026-01-01T00:00:00",
                        "state": "COMPLETED",
                        "source": "sacct",
                    }
                    | fact,
                }
                for index, fact in enumerate(facts, 1)
            ],
        }
    )


async def test_page_collision_rolls_back_entire_page_and_namespaces_hubs(settings_factory):
    store = await Store.open(settings_factory().database_path)
    try:
        hub = str(uuid4())
        await persist_page(store, page(hub, {}, {}))
        rich_page = page(hub, {}, {"state": "FAILED"}, {})
        # Process a new event before the conflict to verify a real partial rollback.
        conflict = Page.model_validate(
            {
                "hub_id": hub,
                "claim": uuid4().hex,
                "deliveries": [
                    rich_page.deliveries[2],
                    rich_page.deliveries[1].model_copy(update={"id": 4}),
                ],
            }
        )
        with pytest.raises(ValueError, match="Conflicting Slurm event"):
            await persist_page(store, conflict)
        assert (await store._one("SELECT count(*) FROM slurm_bridge_deliveries"))[0] == 2
        assert (await store._one("SELECT count(*) FROM slurm_bridge_events"))[0] == 2
        await persist_page(store, page(str(uuid4()), {"state": "FAILED"}))
        assert (await store._one("SELECT count(*) FROM slurm_bridge_events"))[0] == 3
    finally:
        await store.close()


async def test_bridge_hint_uses_registered_attempt_and_scheduler_confirmation(cluster):
    plans, context, tmp = cluster
    store = await Store.open(tmp / "bridge.sqlite3")
    session = (await store.create_session("bridge"))["id"]
    context = type(context)(session, context.run_id)
    plans.store = store
    settings = plans.chat.service.settings.model_copy(
        update={
            "slurm_bridge_queue_hosts": {"cluster-a": "test"},
        }
    )
    bridge = plans.slurm_bridge = SlurmBridge(settings, store)
    bridge.hub_id = str(uuid4())
    try:
        identifier = await draft(plans, context, [job(tmp)])
        await plans.tick_plan(session, identifier)
        plan = plans.chat.load(session, "plans", identifier)
        state = plan["runtime"]["compute"]
        state["polled_at"] = utc_now()
        plans.save(session, plan)
        receipt = state["job"]
        fact = {"job_id": receipt["scheduler_job_id"], "name": receipt["name"]}
        await persist_page(store, page(bridge.hub_id, fact | {"name": "older-attempt"}))
        await plans.tick_plan(session, identifier)
        assert not plans.chat.load(session, "plans", identifier)["job_events"]
        await persist_page(store, page(bridge.hub_id, fact | {"name": "older-attempt"}, fact))
        (tmp / "accounting_down").touch()
        await plans.tick_plan(session, identifier)
        pending = plans.chat.load(session, "plans", identifier)
        assert pending["runtime"]["compute"]["bridge_observation_seq"]
        assert not pending["job_events"]  # A hint cannot manufacture a terminal fact.
        (tmp / "accounting_down").unlink()
        pending["runtime"]["compute"].pop("polled_at")
        plans.save(session, pending)
        await plans.tick_plan(session, identifier)
        await plans.tick_plan(session, identifier)
        finished = plans.chat.load(session, "plans", identifier)
        assert finished["status"] == "completed"
        assert len(finished["job_events"]) == 1
        assert await WaitLedger(store).pending(session, finished)
        assert len(json.loads((tmp / "scheduler.json").read_text())) == 1
    finally:
        await plans.close()
        await store.close()


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3] / "监控系统"
    if not (root / "queue_service.py").is_file():
        pytest.skip("Independent queue monitor source checkout is unavailable")
    monkeypatch.syspath_prepend(str(root))
    from queue_monitor.storage import Store as SourceStore
    from queue_service import handler_for

    source_store = SourceStore(tmp_path / "source.sqlite3")
    source_store.register_queue("cluster-a", {"transport": "local"})
    source_store.register_consumer("bridge", {"kind": "platform_bridge"})
    source_store.subscribe("bridge", "cluster-a", {})
    for job_id in ("101", "102", "103"):
        source_store.record(
            "cluster-a",
            {
                "job_id": job_id,
                "submitted": "2026-01-01T00:00:00",
                "state": "COMPLETED",
                "source": "sacct",
            },
        )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(source_store, "test-token"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield source_store, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_platform_runtime_starts_and_closes_configured_bridge(
    source, settings_factory, tmp_path, monkeypatch
):
    source_store, url = source
    acknowledged = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_finish = source_store.finish

    def finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        loop.call_soon_threadsafe(acknowledged.set)

    monkeypatch.setattr(source_store, "finish", finish)
    token_file = tmp_path / "test-token"
    token_file.write_text("test-token")
    settings = settings_factory(
        slurm_bridge_url=url, slurm_bridge_consumer="bridge", slurm_bridge_token_file=token_file
    )
    async with PlatformRuntime(settings) as runtime:
        await asyncio.wait_for(acknowledged.wait(), timeout=5)
        assert [row["status"] for row in source_store.inbox("bridge")] == ["delivered"] * 3
        count = await runtime.store._one("SELECT count(*) FROM slurm_bridge_events")
        assert count[0] == 3
    assert runtime.chat.plans.slurm_bridge is None


@pytest.mark.parametrize("boundary", ["before_commit", "before_ack", "after_ack"])
async def test_ev02_redelivery_after_worker_process_exit(
    boundary, source, settings_factory, tmp_path
):
    source_store, url = source
    token_file = tmp_path / "test-token"
    token_file.write_text("test-token")
    settings = settings_factory(
        slurm_bridge_url=url, slurm_bridge_consumer="bridge", slurm_bridge_token_file=token_file
    )
    worker = tmp_path / "bridge_worker.py"
    worker.with_suffix(".json").write_text(
        json.dumps(
            {
                "boundary": boundary,
                "url": url,
                "token_file": str(token_file),
                "database_path": str(settings.database_path),
            }
        )
    )
    worker.write_text("""import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from chemistry_agent.config import Settings
from chemistry_agent.platform import slurm_bridge
from chemistry_agent.store import Store

async def main() -> None:
    config = json.loads(Path(__file__).with_suffix(".json").read_text())
    settings = Settings(
        _env_file=None, database_path=config["database_path"], slurm_bridge_url=config["url"],
        slurm_bridge_consumer="bridge", slurm_bridge_token_file=config["token_file"],
    )
    store = await Store.open(settings.database_path)
    original_transaction = store.transaction
    original_persist = slurm_bridge.persist_page

    @asynccontextmanager
    async def interrupt_transaction():
        async with original_transaction():
            yield
            os._exit(23)

    async def interrupt_ack(destination: Store, page: slurm_bridge.Page) -> None:
        await original_persist(destination, page)
        os._exit(23)

    if config["boundary"] == "before_commit":
        store.transaction = interrupt_transaction
    if config["boundary"] == "before_ack":
        slurm_bridge.persist_page = interrupt_ack
    async with httpx.AsyncClient(trust_env=False) as client:
        await slurm_bridge.SlurmBridge(settings, store).transfer_page(client, limit=2)
    os._exit(23)

if __name__ == "__main__":
    asyncio.run(main())
""")
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", str(worker))
    try:
        assert await asyncio.wait_for(process.wait(), 10) == 23
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    store = await Store.open(settings.database_path)
    try:
        assert (await store._one("SELECT count(*) FROM slurm_bridge_events"))[0] == (
            0 if boundary == "before_commit" else 2
        )
        source_rows = source_store.inbox("bridge")
        assert [row["status"] for row in source_rows] == (
            ["delivered", "delivered", "pending"]
            if boundary == "after_ack"
            else ["leased", "leased", "pending"]
        )
        old_claim = source_rows[0]["claim"]
        with source_store.connect() as db:
            db.execute("UPDATE deliveries SET lease_until=0 WHERE status='leased'")
        async with httpx.AsyncClient(trust_env=False) as client:
            bridge = SlurmBridge(settings, store)
            if boundary != "after_ack":
                assert await bridge.transfer_page(client, limit=2) == 2
                assert source_store.inbox("bridge")[0]["claim"] != old_claim
            assert await bridge.transfer_page(client, limit=2) == 1
            assert await bridge.transfer_page(client, limit=2) == 0
        assert [row["status"] for row in source_store.inbox("bridge")] == ["delivered"] * 3
        query = "SELECT job_id FROM slurm_bridge_events ORDER BY seq"
        async with store.db.execute(query) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["101", "102", "103"]
        assert (await store._one("SELECT count(*) FROM slurm_bridge_deliveries"))[0] == 3
    finally:
        await store.close()
