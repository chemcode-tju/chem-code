import asyncio
import json
from pathlib import Path

import pytest

from chemistry_agent.backends.codex_home import CodexHomes
from chemistry_agent.backends.transport import CodexTransport
from chemistry_agent.errors import AgentError


def legacy_rollout(source: Path, active: bool = False) -> Path:
    path = source / "sessions/2026/11/13/rollout-example-thread1.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "thread1"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn1"}},
    ]
    if not active:
        records.append(
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn1"}}
        )
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return path


def test_home_isolates_mutable_state_and_references_existing_auth(tmp_path):
    source = tmp_path / "desktop"
    source.mkdir()
    (source / "auth.json").write_text("test-auth")
    (source / "config.toml").write_text('model = "example"')
    (source / "state.sqlite").write_text("desktop state")
    homes = CodexHomes(tmp_path / "platform", source)
    target = homes.prepare("session1")
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "auth.json").is_symlink()
    assert not (target / "state.sqlite").exists()
    (target / "config.toml").write_text('model = "changed"')
    assert (source / "config.toml").read_text() == 'model = "example"'
    (source / "auth.json").write_text("rotated")
    assert (target / "auth.json").read_text() == "rotated"
    assert homes.prepare("session2") != target


def test_legacy_import_is_a_one_time_private_snapshot(tmp_path):
    source = tmp_path / "desktop"
    original = legacy_rollout(source)
    homes = CodexHomes(tmp_path / "platform", source)
    target = homes.prepare("session1")
    homes.import_thread(target, "thread1")
    snapshot = target / original.relative_to(source)
    assert snapshot.read_bytes() == original.read_bytes()
    snapshot.write_text(snapshot.read_text() + "platform changes\n")
    homes.import_thread(target, "thread1")
    assert snapshot.read_text().endswith("platform changes\n")
    assert "platform changes" not in original.read_text()


def test_active_legacy_turn_cannot_be_copied_and_replayed(tmp_path):
    source = tmp_path / "desktop"
    legacy_rollout(source, active=True)
    homes = CodexHomes(tmp_path / "platform", source)
    target = homes.prepare("session1")
    with pytest.raises(AgentError, match="未结束"):
        homes.import_thread(target, "thread1")
    assert not (target / "sessions").exists()


async def test_platform_process_lock_is_per_conversation(tmp_path, monkeypatch):
    homes = CodexHomes(tmp_path / "platform", tmp_path / "desktop")

    async def fake_start(self):
        assert self.runtime_home == self.codex_home
        return self

    monkeypatch.setattr(CodexTransport, "start", fake_start)
    first, second, other = [CodexTransport("unused") for _ in range(3)]
    first.codex_home = second.codex_home = homes.prepare("one")
    other.codex_home = homes.prepare("two")
    async with first:
        with pytest.raises(AgentError, match="另一平台进程"):
            async with second:
                pytest.fail("Duplicate writer admitted")
        async with other:
            pass
    async with second:
        pass


async def test_rpc_busy_error_is_actionable():
    rpc = CodexTransport("unused")
    from types import SimpleNamespace

    stream = asyncio.StreamReader()
    rpc.process = SimpleNamespace(stdout=stream)
    future = asyncio.get_running_loop().create_future()
    rpc.pending[1] = future
    stream.feed_data(
        b'{"id":1,"error":{"message":"thread example already has an active writer"}}\n'
    )
    stream.feed_eof()
    await rpc.read()
    with pytest.raises(AgentError) as error:
        await future
    assert error.value.code == "codex_session_busy"
