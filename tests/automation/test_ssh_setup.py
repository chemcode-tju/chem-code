import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

asyncssh = pytest.importorskip("asyncssh")
pytest.importorskip("aiida")
from aiida import orm  # noqa: E402
from aiida.tools.pytest_fixtures.configuration import (  # noqa: E402,F401
    aiida_config_factory,
    aiida_profile_factory,
)

from chemistry_agent.automation import backend, ssh_connection  # noqa: E402
from chemistry_agent.automation.models import ServerCreate  # noqa: E402
from chemistry_agent.automation.ssh_profiles import load_profile, shell_profiles  # noqa: E402
from chemistry_agent.errors import AgentError  # noqa: E402
from chemistry_agent.runtime.shell import ShellInput, remote_shell_tool, shell_grant  # noqa: E402
from chemistry_agent.tools import ToolContext  # noqa: E402


class TestEndpoint(asyncssh.SSHServer):
    __test__ = False

    def __init__(self, secret: str, public) -> None:
        self.secret, self.public = secret, public

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "research" and password == self.secret

    def validate_public_key(self, username: str, key) -> bool:
        return username == "research" and key == self.public


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    private = tmp_path / ".ssh" / "id_ed25519"
    private.parent.mkdir(mode=0o700)
    identity = asyncssh.generate_private_key("ssh-ed25519")
    private.write_bytes(identity.export_private_key())
    private.chmod(0o600)
    hostkey = asyncssh.generate_private_key("ssh-ed25519")
    secret = str(uuid4())
    service = {}

    async def command(process):
        process.stdout.write("transport-ok\n")
        process.exit(0)

    async def serve():
        server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[hostkey],
            process_factory=command,
            sftp_factory=asyncssh.SFTPServer,
            server_factory=lambda: TestEndpoint(secret, identity.convert_to_public()),
        )
        stop = asyncio.Event()
        service.update(port=server.get_port(), stop=lambda: loop.call_soon_threadsafe(stop.set))
        ready.set()
        await stop.wait()
        server.close()
        await server.wait_closed()

    ready = threading.Event()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        yield service["port"], hostkey, private, secret
    finally:
        service["stop"]()
        thread.join(5)
        loop.close()


@pytest.mark.parametrize("mode", ["password", "key", "alias"])
def test_settings_login_is_shared_by_shell_and_aiida(
    endpoint,
    tmp_path,
    monkeypatch,
    mode,
    aiida_config_factory,  # noqa: F811
    aiida_profile_factory,  # noqa: F811
):
    port, hostkey, identity, secret = endpoint
    root = tmp_path / "profile"
    root.mkdir()
    server = ServerCreate(
        label=f"cluster-{mode}",
        hostname="127.0.0.1",
        port=port,
        username="research",
        workdir="/scratch",
        auth_mode=mode if mode != "alias" else "key",
        identity_file=str(identity) if mode == "key" else None,
    )
    if mode == "alias":
        (tmp_path / ".ssh/config").write_text(
            "Host loopback-ssh\n HostName 127.0.0.1\n User research\n"
            f" Port {port}\n IdentityFile {identity}\n"
        )
        # OpenSSH uses the OS account home even if a test overrides HOME.
        run = subprocess.run

        def resolve_fixture(args, **kwargs):
            if args[:2] == ["ssh", "-G"]:
                args = args[:2] + ["-F", str(tmp_path / ".ssh/config")] + args[2:]
            return run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", resolve_fixture)
        server = ServerCreate(label="cluster-alias", hostname="loopback-ssh", workdir="/scratch")
    secrets_read = []

    def read_secret(identifier):
        secrets_read.append(identifier)
        return secret

    monkeypatch.setattr(ssh_connection, "load_password", read_secret)
    monkeypatch.setenv("AIIDA_PATH", str(root))
    target, _, result = asyncio.run(ssh_connection.discover(root, server))
    assert target.port == port and target.username == "research"
    assert not result["trusted"] and not secrets_read
    with pytest.raises(AgentError, match="独立核对"):
        asyncio.run(ssh_connection.pin_server(root, server))
    with pytest.raises(AgentError, match="不一致"):
        asyncio.run(ssh_connection.pin_server(root, server, "SHA256:wrong"))
    asyncio.run(ssh_connection.pin_server(root, server, hostkey.get_fingerprint()))
    with aiida_config_factory(root) as config, aiida_profile_factory(config):
        monkeypatch.setenv("AIIDA_PATH", str(root))
        saved = backend.create_server(server.model_dump(mode="json"))
        assert backend.create_server(server.model_dump(mode="json"))["id"] == saved["id"]
        profile = load_profile(root, saved["id"])
        assert shell_profiles(root)[saved["shell_alias"]] == saved["id"]
        computer = orm.load_computer(uuid=saved["id"])
        assert secret not in json.dumps(computer.get_configuration()) + json.dumps(profile)
        assert not secrets_read
        with computer.get_transport() as transport:
            code, stdout, _ = transport.exec_command_wait("hostname")
            assert code == 0 and stdout.strip() == "transport-ok"
        token = shell_grant.set(saved["shell_alias"])
        try:
            command = remote_shell_tool((), root)
            result = asyncio.run(
                command.handler(ShellInput(command="hostname"), ToolContext("s", "r"))
            )
            assert result["ok"] and result["stdout"].strip() == "transport-ok"
        finally:
            shell_grant.reset(token)
        assert bool(secrets_read) == (mode == "password")
        assert orm.QueryBuilder().append(orm.CalcJobNode).count() == 0


async def test_known_host_on_other_port_is_not_authority(endpoint, tmp_path):
    port, hostkey, _, _ = endpoint
    config = ServerCreate(
        label="x",
        hostname="127.0.0.1",
        port=port,
        username="research",
        auth_mode="password",
        workdir="/scratch",
    )
    hosts = tmp_path / ".ssh/known_hosts"
    hosts.write_text(f"127.0.0.1 {hostkey.export_public_key().decode()}")
    assert not (await ssh_connection.discover(tmp_path, config))[2]["trusted"]
    other = asyncssh.generate_private_key("ssh-ed25519")
    hosts.write_text(f"[127.0.0.1]:{port} {other.export_public_key().decode()}")
    result = (await ssh_connection.discover(tmp_path, config))[2]
    assert result["changed"] and not result["trusted"]
    with pytest.raises(AgentError, match="主机密钥已改变"):
        await ssh_connection.pin_server(tmp_path, config, result["fingerprint"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "evil -oProxyCommand=x"),
        ("identity_file", "/tmp/key"),
        ("identity_file", "~/.ssh/id_ed25519.pub"),
        ("password", "hidden"),
    ],
)
def test_server_configuration_rejects_inline_secrets_and_unsafe_addresses(field, value):
    body = dict(
        label="server", hostname="cluster", username="alice", auth_mode="key", workdir="/scratch"
    )
    with pytest.raises(ValidationError):
        ServerCreate.model_validate(body | {field: value})


async def test_password_endpoint_is_local_and_not_an_agent_tool(
    tmp_path, settings_factory, monkeypatch
):
    import httpx

    from chemistry_agent.api import create_app
    from chemistry_agent.automation import ssh_keychain
    from chemistry_agent.automation.ssh_profiles import profile_path, write_private

    app = create_app(settings_factory(automation_enabled=True))
    identifier = uuid4()
    path = profile_path(app.state.settings.aiida_path, str(identifier))
    write_private(
        path, {"id": str(identifier), "auth_mode": "password", "shell_alias": "chem-test"}
    )
    saved = {}

    def change(key, value):
        if value is None:
            saved.pop(key, None)
        else:
            saved[key] = value

    monkeypatch.setattr(ssh_keychain, "set_password", change)
    secret = str(uuid4())
    uri = f"/api/automation/servers/{identifier}/password"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        forbidden = await client.put(
            uri, json={"password": secret}, headers={"Origin": "https://other.test"}
        )
        assert forbidden.status_code == 403 and not saved
        response = await client.put(uri, json={"password": secret})
        assert response.status_code == 200 and saved[str(identifier)] == secret
        assert secret not in response.text and secret not in path.read_text()
        assert (await client.delete(uri)).status_code == 200 and not saved
        names = {x["function"]["name"] for x in (await client.get("/tools")).json()}
        assert "ssh_server_register" in names and not any("password" in name for name in names)
        direct = await client.post(
            "/tools/ssh_server_register/invoke",
            json={
                "arguments": {"label": "test", "hostname": "server.local", "workdir": "/scratch"}
            },
        )
        assert not direct.json()["success"]


@pytest.mark.skipif(
    shutil.which("ssh-agent") is None or shutil.which("ssh-add") is None,
    reason="OpenSSH agent not installed",
)
async def test_agent_mode_uses_only_loaded_identities(endpoint, tmp_path, monkeypatch):
    port, hostkey, identity, _ = endpoint
    body = ServerCreate(
        label="agent",
        hostname="127.0.0.1",
        auth_mode="key",
        username="research",
        port=port,
        workdir="/scratch",
    )
    await ssh_connection.pin_server(tmp_path, body, hostkey.get_fingerprint())
    profile = {
        "id": str(uuid4()),
        "server": body.model_dump(),
        "hostname": body.hostname,
        "port": port,
        "username": "research",
    }
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        socket = str(Path(directory) / "agent.sock")
        monkeypatch.setenv("SSH_AUTH_SOCK", socket)
        with pytest.raises(AgentError) as unavailable:
            await ssh_connection.connect(tmp_path, profile)
        assert unavailable.value.code == "ssh_agent_missing"
        process = await asyncio.create_subprocess_exec(
            "ssh-agent",
            "-D",
            "-a",
            socket,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            for _ in range(80):
                if await asyncio.to_thread(Path(socket).exists):
                    break
                await asyncio.sleep(0.025)
            with pytest.raises(AgentError) as empty:
                await ssh_connection.connect(tmp_path, profile)
            assert empty.value.code == "ssh_agent_empty"
            loaded = await asyncio.create_subprocess_exec(
                "ssh-add",
                str(identity),
                env=os.environ.copy(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert await loaded.wait() == 0
            async with await ssh_connection.connect(tmp_path, profile) as client:
                assert (await client.run("hostname")).stdout.strip() == "transport-ok"
        finally:
            process.terminate()
            await process.wait()
