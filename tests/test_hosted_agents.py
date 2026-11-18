"""Real SSH protocol transport with fake native Agents; never contacts an HPC cluster."""

import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.backends.service import BackendSettings
from chemistry_agent.backends.transport import CodexTransport
from chemistry_agent.errors import AgentError
from chemistry_agent.hosting import connection as connections
from chemistry_agent.hosting.native import (
    chosen_directory,
    history_name,
    native_scope,
    save_history,
)
from chemistry_agent.hosting.registry import AgentInstance
from chemistry_agent.workspace_files import WorkspaceFiles


@pytest.fixture
def hosted_aliases(monkeypatch):
    monkeypatch.setattr(
        "chemistry_agent.environments.registry.discover_aliases",
        lambda: [{"alias": "cluster-a"}, {"alias": "cluster-b"}],
    )


def test_locations_belong_to_each_conversation(settings_factory, hosted_aliases, tmp_path):
    settings = settings_factory(ssh_hosts="cluster-a,cluster-b")
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 1)
    ) as client:
        existing = client.post("/sessions", json={}).json()["id"]
        instance = {
            "id": "cluster-a-codex",
            "name": "Codex CPU",
            "backend": "codex",
            "host_id": "cluster-a",
            "executable": "/remote/codex",
            "model": "model-cpu",
        }
        assert client.put("/api/hosting/agents/cluster-a-codex", json=instance).status_code == 200
        assert client.put("/api/hosting/agents/cluster-a-codex", json=instance).status_code == 200
        assert client.put("/api/hosting/spaces/cu-water", json={}).status_code == 404
        assert "spaces" not in client.get("/api/hosting").json()
        current = client.get("/api/agents/selection").json()
        assert current["host_id"] == "local" and current["directory"] == ""
        session = client.post("/sessions", json={}).json()["id"]
        choice = {
            "instance_id": "cluster-a-codex",
            "backend": "codex",
            "model": "model-cpu",
            "permission": "user",
            "directory": "~/research",
            "monitor_host": "cluster-b",
        }
        endpoint = f"/api/agents/sessions/{session}"
        assert client.put(endpoint, json=choice).status_code == 200
        assert client.get(endpoint).json()["directory"] == "~/research"
        assert client.get(f"/api/agents/sessions/{existing}").json()["host_id"] == "local"
        assert client.get("/api/agents/selection").json()["directory"] == ""
        backend = client.app.state.agent_backends
        client.portal.call(partial(backend.update_thread, session, external_thread_id="native-1"))
        client.put("/api/hosting/agents/cluster-a-codex", json=instance | {"executable": "/other/codex"})
        monitor_only = {key: value for key, value in choice.items() if key != "directory"}
        assert (
            client.put(endpoint, json=monitor_only | {"monitor_host": "local"}).status_code == 200
        )
        config = client.portal.call(app_binding, client.app, session)
        assert config["working_directory"] == "~/research"
        assert config["codex_executable"] == "/remote/codex"
        assert client.portal.call(backend.binding, session)["external_thread_id"] == "native-1"
        assert client.put(endpoint, json=choice | {"directory": ""}).status_code == 200
        assert client.get(endpoint).json()["directory"] == ""
        assert client.portal.call(backend.binding, session)["external_thread_id"] is None
        assert client.put(endpoint, json=choice | {"directory": "relative/path"}).status_code == 422
        # Browsing reads names; choosing a new path does not create it before a turn.
        folder = tmp_path / "new-directory"
        listing = client.get("/api/hosting/hosts/local/directories", params={"path": str(folder)})
        assert listing.json()["exists"] is False and not folder.exists()


async def app_binding(app, session):
    return json.loads((await app.state.agent_backends.binding(session))["config"])


async def test_legacy_folders_and_shared_folder_history_are_preserved(tmp_path):
    legacy = BackendSettings(workspace_id="old-space", workspace_path=str(tmp_path / "old"))
    assert chosen_directory(legacy, "session-a") == str(tmp_path / "old/session-a")
    folder = tmp_path / "existing-project"
    folder.mkdir()
    (folder / "platform-history.json").write_text("existing project file")
    config = BackendSettings(working_directory=str(folder))
    router = SimpleNamespace(root=tmp_path / "default-workspaces")
    for session in ["session-a", "session-b"]:
        assert await save_history(router, config, session, [{"content": session}]) == folder
        assert json.loads((folder / history_name(config, session)).read_text()) == [
            {"content": session}
        ]
    assert (folder / "platform-history.json").read_text() == "existing project file"
    assert not (folder / "session-a").exists()


NATIVE_AGENT = """import json,os,sys
from pathlib import Path
def send(message):
    print(json.dumps(message),flush=True)
for line in sys.stdin:
    message=json.loads(line)
    method=message.get("method")
    if "id" not in message: continue
    identifier=message["id"]
    if not method: continue
    result={}
    if method=="account/read": result={"account":{"type":"chatgpt"}}
    if method=="model/list":
        result={"data":[{"model":"remote-model","displayName":"Remote model",
            "supportedReasoningEfforts":[{"reasoningEffort":"high"}]}]}
    if method in ("thread/start","thread/resume"):
        assert "workspace" in message["params"]["cwd"] or "research" in message["params"]["cwd"]
        result={"thread":{"id":"remote-thread"}}
    if method=="turn/start": result={"turn":{"id":"remote-turn"}}
    send({"id":identifier,"result":result})
    if method=="turn/start":
        Path("remote-result.txt").write_text("remote output")
        send({"method":"item/completed","params":{"threadId":"remote-thread","turnId":"remote-turn",
            "item":{"id":"native-file","type":"fileChange","status":"completed",
                "changes":[{"path":str(Path.cwd()/"remote-result.txt"),"kind":{"type":"add"},"diff":""}]}}})
        counter=Path(os.environ["CODEX_HOME"])/"test-turns.txt"
        counter.write_text((counter.read_text() if counter.exists() else "")+"turn\\n")
        send({"method":"item/agentMessage/delta","params":{"delta":"远端执行完成"}})
        send({"method":"turn/completed","params":{"turn":{"id":"remote-turn","status":"completed"}}})
    if method=="thread/compact/start":
        send({"method":"thread/compacted","params":{"threadId":"remote-thread"}})
        send({"method":"turn/completed","params":{"turn":{"status":"completed"}}})
"""


@pytest.fixture
async def ssh_fixture(tmp_path, monkeypatch, hosted_aliases):
    asyncssh = pytest.importorskip("asyncssh")
    remote_home = tmp_path / "ssh-account"
    remote_home.mkdir()
    executable = remote_home / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + NATIVE_AGENT)
    executable.chmod(0o700)
    source = connections.HELPER.replace(
        'if __name__ == "__main__":',
        f"Path.home = classmethod(lambda cls: Path({str(remote_home)!r}))\n"
        'if __name__ == "__main__":',
    )
    monkeypatch.setattr(connections, "HELPER", source)

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False

        def unix_server_requested(self, path):
            return True

    async def serve(process):
        words = shlex.split(process.command)
        if words[:2] == ["bash", "-lc"]:
            words = shlex.split(words[2])
        # Execute only our helper in a disposable account; use the test interpreter.
        if words[0] == "python3":
            words[0] = sys.executable
        child = await asyncio.create_subprocess_exec(
            *words,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def copy(reader, writer):
            try:
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
            finally:
                with suppress(Exception):
                    writer.write_eof()

        tasks = [
            asyncio.create_task(copy(process.stdin, child.stdin)),
            asyncio.create_task(copy(child.stdout, process.stdout)),
            asyncio.create_task(copy(child.stderr, process.stderr)),
        ]
        try:
            await child.wait()
            await asyncio.gather(*tasks[1:])
            process.exit(child.returncode)
        finally:
            for task in tasks:
                task.cancel()
            if child.returncode is None:
                child.kill()
                await child.wait()

    server = await asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=serve,
        encoding=None,
        sftp_factory=asyncssh.SFTPServer,
    )

    @asynccontextmanager
    async def connect(hosting, host):
        hosting.host(host)
        async with asyncssh.connect(
            "127.0.0.1", server.get_port(), known_hosts=None, config=None, username="fixture"
        ) as ssh:
            yield ssh

    monkeypatch.setattr("chemistry_agent.hosting.native.connection", connect)
    monkeypatch.setattr(connections, "connection", connect)
    try:
        yield remote_home, executable
    finally:
        server.close()
        await server.wait_closed()


async def test_remote_chat_resume_compact_and_publish(settings_factory, ssh_fixture, monkeypatch):
    home, executable = ssh_fixture
    settings = settings_factory(ssh_hosts="cluster-a,cluster-b")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://127.0.0.1",
        ) as client:
            instance = AgentInstance(
                id="cluster-a-codex",
                name="Remote",
                host_id="cluster-a",
                backend="codex",
                executable=str(executable),
            )
            app.state.agent_backends.hosting.save(instance)
            session = (await client.post("/sessions", json={})).json()["id"]
            choice = {
                "backend": "codex",
                "instance_id": "cluster-a-codex",
                "directory": str(home / "research"),
                "model": "remote-model",
                "permission": "full",
                "monitor_host": "local",
            }
            response = await client.put(f"/api/agents/sessions/{session}", json=choice)
            assert response.status_code == 200, response.text
            models = await client.get("/api/hosting/agents/cluster-a-codex/models")
            assert models.status_code == 200, models.text
            assert models.json()["models"][0]["id"] == "remote-model"
            research = home / "research"
            research.mkdir(exist_ok=True)
            (research / "preexisting.txt").write_text("not generated by this conversation")

            async def incomplete_snapshot(session):
                return {"complete": False, "files": {}}

            monkeypatch.setattr(app.state.agent_backends, "output_snapshot", incomplete_snapshot)
            for _ in range(2):
                reply = await client.post(
                    "/chat", json={"session_id": session, "message": "只验证协议"}
                )
                assert reply.status_code == 200, reply.text
                assert reply.json()["answer"] == "远端执行完成"
            backend = app.state.agent_backends
            binding = await backend.binding(session)
            assert binding["external_thread_id"] == "remote-thread"
            config = BackendSettings.model_validate_json(binding["config"])
            moved_monitor = await client.put(
                f"/api/agents/sessions/{session}", json=choice | {"monitor_host": "cluster-b"}
            )
            assert moved_monitor.status_code == 200, moved_monitor.text
            after_monitor = await backend.binding(session)
            assert after_monitor["external_thread_id"] == "remote-thread"
            assert json.loads(after_monitor["config"])["working_directory"] == str(
                home / "research"
            )
            async with native_scope(backend, config, session, "codex"):
                await backend.codex.context_operation(session, binding, "compact")
            turns = list(home.glob(".local/state/chem-code/*/agents/cluster-a-codex/*/test-turns.txt"))
            assert len(turns) == 1 and turns[0].read_text().splitlines() == ["turn", "turn"]
            assert (backend.root / session / "remote-result.txt").read_text() == "remote output"
            outputs = await app.state.workspace_files.publish(session, [])
            assert len(outputs["files"]) == 1
            assert not (backend.root / session / "preexisting.txt").exists()
            assert outputs["files"][0]["source_path"] == str(
                home / "research" / "remote-result.txt"
            )
            restored = WorkspaceFiles(backend.store, backend.root)
            await restored.initialize()
            assert (await restored.list(session))[0]["sources"] == outputs["files"][0]["sources"]
            async with native_scope(backend, config, session, "codex") as remote:
                skill = home / "skill-fixture"
                skill.mkdir()
                (skill / "SKILL.md").write_text("Research skill")
                (skill / "script.py").write_text("print('initial')")
                destination = Path(await remote.materialize_skill("test-skill", skill))
                assert destination.is_relative_to(home / "research")
                (destination / "script.py").write_text("print('agent edit')")
                await remote.materialize_skill("test-skill", skill)
                assert (destination / "script.py").read_text() == "print('agent edit')"
                with pytest.raises(AgentError, match="远端文件不存在"):
                    await remote.sync_outputs(["not-created.xyz"])
                async with CodexTransport(str(executable)):
                    with pytest.raises(AgentError, match="BlockingIOError"):
                        async with CodexTransport(str(executable)):
                            pytest.fail("Two writers acquired the same remote home")
            # A replay is settled from the receipt before opening a new SSH connection.
            receipt = await backend.store._one(
                "SELECT * FROM backend_deliveries WHERE session_id=? LIMIT 1", (session,)
            )

            @asynccontextmanager
            async def forbidden_connection(*args):
                raise AssertionError("A duplicate delivery must not contact the remote Agent")
                yield

            monkeypatch.setattr("chemistry_agent.hosting.native.native_scope", forbidden_connection)
            replay = await backend.run([], None, session_id=session, run_id=receipt["run_id"])
            assert replay["messages"][-1]["content"] == "远端执行完成"
            async with backend.store.transaction():
                await backend.store.db.execute(
                    "UPDATE backend_deliveries SET state='uncertain' WHERE id=?", (receipt["id"],)
                )
            with pytest.raises(AgentError, match="不会重复发送"):
                await backend.run([], None, session_id=session, run_id=receipt["run_id"])


ACP_AGENT = """import json,sys,subprocess,os
bridge=None
def send(message): print(json.dumps(message),flush=True)
options=[{"id":"mode","category":"mode","options":[{"value":"default"},{"value":"auto"}]}]
for line in sys.stdin:
    message=json.loads(line);method=message.get("method");identifier=message.get("id")
    if identifier is None: continue
    result={}
    if method=="initialize": result={"protocolVersion":1,"agentCapabilities":{"loadSession":True}}
    if method in ("session/new","session/load"):
        bridge=(message["params"].get("mcpServers") or [None])[0]
        result={"sessionId":"remote-acp","configOptions":options}
    if method=="session/set_config_option": result={"configOptions":options}
    if method=="session/prompt":
        env={**os.environ,**{v["name"]:v["value"] for v in bridge["env"]}}
        response=subprocess.run([bridge["command"],*bridge["args"]],input=json.dumps({"id":1,"method":"tools/call","params":{"name":"molecular_properties","arguments":{"smiles":"CO"}}})+"\\n",text=True,capture_output=True,env=env,timeout=15)
        assert response.returncode==0,response.stderr
        assert "CH4O" in response.stdout,response.stdout
        send({"method":"session/update","params":{"sessionId":"remote-acp","update":{
            "sessionUpdate":"agent_message_chunk",
            "content":{"type":"text","text":"远端 MCP CH4O"}}}})
        result={"stopReason":"end_turn"}
    send({"id":identifier,"result":result})
"""


async def test_remote_acp_mcp_tools_and_session_resume(settings_factory, ssh_fixture):
    home, _ = ssh_fixture
    executable = home / "fake-agy"
    executable.write_text(f"#!{sys.executable}\n" + ACP_AGENT)
    executable.chmod(0o700)
    app = create_app(settings_factory(ssh_hosts="cluster-a"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://127.0.0.1",
        ) as client:
            backends = app.state.agent_backends
            backends.hosting.save(
                AgentInstance(
                    id="cluster-a-agy",
                    name="Remote ACP",
                    host_id="cluster-a",
                    backend="antigravity",
                    executable=str(executable),
                )
            )
            session = (await client.post("/sessions", json={})).json()["id"]
            response = await client.put(
                f"/api/agents/sessions/{session}",
                json={"backend": "antigravity", "instance_id": "cluster-a-agy", "permission": "full"},
            )
            assert response.status_code == 200, response.text
            for _ in range(2):
                reply = await client.post("/chat", json={"session_id": session, "message": "甲醇"})
                assert reply.status_code == 200, reply.text
                assert reply.json()["answer"] == "远端 MCP CH4O"
                assert reply.json()["tool_calls"][0]["name"] == "molecular_properties"
            assert (await backends.binding(session))["external_thread_id"] == "remote-acp"
