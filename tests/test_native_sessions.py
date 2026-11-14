"""Cross-process ownership, native history migration and unchanged capabilities."""

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chemistry_agent.backends.acp_transport import ACPTransport
from chemistry_agent.backends.antigravity import AntigravityBackend
from chemistry_agent.backends.antigravity_home import AntigravityHomes
from chemistry_agent.backends.codex import CodexBackend
from chemistry_agent.backends.runtime import NativeSessions, SessionLease
from chemistry_agent.backends.service import BackendSettings
from chemistry_agent.backends.transport import CodexTransport
from chemistry_agent.errors import AgentError


@pytest.mark.parametrize("transport_type", [CodexTransport, ACPTransport])
async def test_native_transport_failure_releases_lease_and_other_sessions_parallel(
    tmp_path, monkeypatch, transport_type
):
    sessions = NativeSessions(tmp_path)

    async def start(self):
        return self

    monkeypatch.setattr(transport_type, "start", start)

    def transport(session):
        rpc = (
            transport_type("unused")
            if transport_type is CodexTransport
            else transport_type("unused", [], str(tmp_path))
        )
        return sessions.configure(rpc, "test", session)

    async with transport("one"):
        with pytest.raises(AgentError, match="另一平台进程"):
            async with transport("one"):
                pytest.fail("second writer")
        async with transport("two"):
            pass
    async with transport("one"):
        pass

    async def fail(self):
        raise RuntimeError("initialize failed")

    monkeypatch.setattr(transport_type, "start", fail)
    with pytest.raises(RuntimeError):
        async with transport("one"):
            pass
    monkeypatch.setattr(transport_type, "start", start)
    async with transport("one"):
        pass


def test_lock_survives_owner_death_until_native_child_exits(tmp_path):
    home = tmp_path / "session"
    # The short-lived host exits without cleanup, leaving a native child alive.
    code = f"""
import os, subprocess, sys
from pathlib import Path
from chemistry_agent.backends.runtime import SessionLease
lease = SessionLease(Path({str(home)!r}), "test")
lease.acquire()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
    pass_fds=lease.descriptors, start_new_session=True,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    pid = int(result.stdout)
    replacement = SessionLease(home, "test")
    try:
        with pytest.raises(AgentError):
            replacement.acquire()
    finally:
        os.killpg(pid, signal.SIGKILL)
    # Wait on lock release, not a TTL or a guessed PID identity.
    import time

    for _ in range(100):
        try:
            replacement.acquire()
            break
        except AgentError:
            time.sleep(0.01)
    else:
        pytest.fail("lock remained held after native child exited")
    replacement.close()


async def test_cancelled_preparation_finishes_before_releasing_ownership(tmp_path, monkeypatch):
    import threading

    started, release = threading.Event(), threading.Event()
    sessions = NativeSessions(tmp_path)
    rpc = ACPTransport("unused", [], str(tmp_path))

    def prepare():
        started.set()
        release.wait(5)

    sessions.configure(rpc, "test", "one", prepare)
    entering = asyncio.create_task(rpc.__aenter__())
    await asyncio.to_thread(started.wait, 5)
    entering.cancel()
    await asyncio.sleep(0)
    try:
        with pytest.raises(AgentError):
            SessionLease(sessions.home("test", "one"), "test").acquire()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await entering
    lease = SessionLease(sessions.home("test", "one"), "test")
    lease.acquire()
    lease.close()


def legacy_database(source: Path, thread: str = "native") -> Path:
    path = source / "antigravity-cli/conversations" / f"{thread}.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE trajectory_meta(cascade_id TEXT)")
        db.execute("INSERT INTO trajectory_meta VALUES(?)", (thread,))
        db.execute("CREATE TABLE payload(content TEXT)")
        db.execute("INSERT INTO payload VALUES('original history')")
    return path


def test_antigravity_import_only_bound_history_once(tmp_path, monkeypatch):
    source, target = tmp_path / "desktop", tmp_path / "private"
    original = legacy_database(source)
    before = original.read_bytes()
    (source / "settings.json").write_text('{"tools":"custom"}')
    (source / "antigravity-cli/conversation_summaries.db").write_text("unrelated global state")
    homes = AntigravityHomes(source)
    monkeypatch.setattr(homes, "require_idle", lambda path: None)
    homes.prepare(target, "native")
    snapshot = target / original.relative_to(source)
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT content FROM payload").fetchone()[0] == "original history"
        db.execute("UPDATE payload SET content='platform continuation'")
    homes.prepare(target, "native")
    assert original.read_bytes() == before
    assert not (target / "antigravity-cli/conversation_summaries.db").exists()
    assert (target / "settings.json").read_text() == (source / "settings.json").read_text()
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT content FROM payload").fetchone()[0] == "platform continuation"


def test_antigravity_busy_and_missing_history_never_open_empty_thread(tmp_path, monkeypatch):
    source, target = tmp_path / "desktop", tmp_path / "private"
    legacy_database(source)
    homes = AntigravityHomes(source)

    def busy(path):
        raise AgentError("antigravity_session_busy", "busy", 409)

    monkeypatch.setattr(homes, "require_idle", busy)
    with pytest.raises(AgentError, match="busy"):
        homes.prepare(target, "native")
    with pytest.raises(AgentError, match="未找到"):
        homes.prepare(target, "missing")
    assert not (target / "antigravity-cli/conversations/native.db").exists()


def test_isolation_keeps_permissions_tools_and_different_session_homes(tmp_path):
    router = SimpleNamespace(root=tmp_path / "workspaces", sessions=NativeSessions(tmp_path))
    codex = CodexBackend(router, None, "custom instruction")
    config = BackendSettings(permission_mode="full")
    params = codex.session_params(config, tmp_path)
    assert params["sandbox"] == "danger-full-access" and params["approvalPolicy"] == "never"
    first, second = codex.transport("codex", "one"), codex.transport("codex", "two")
    assert first.codex_home != second.codex_home
    agy = AntigravityBackend(router, None, "")
    rpc = agy.transport("agy", ["acp", "--dangerously-skip-permissions"], tmp_path, "one")
    assert "--dangerously-skip-permissions" in rpc.arguments
    assert "--sandbox" not in rpc.arguments
    assert "--process-per-session" in rpc.arguments
    assert rpc.arguments[rpc.arguments.index("--gemini-dir") + 1] == str(rpc.runtime_home)
    assert rpc.runtime_home != first.runtime_home
