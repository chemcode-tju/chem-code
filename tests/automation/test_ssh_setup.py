import asyncio
import os
import shutil
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

asyncssh = pytest.importorskip("asyncssh")
from chemistry_agent.automation import ssh_connection  # noqa: E402
from chemistry_agent.automation.models import ServerCreate  # noqa: E402
from chemistry_agent.errors import AgentError  # noqa: E402


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
    path = profile_path(app.state.settings.platform_path, str(identifier))
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
        assert "ssh_server_register" not in names
        assert "calculation_job_hosts" in names and not any("password" in name for name in names)
        direct = await client.post(
            "/tools/ssh_server_register/invoke",
            json={
                "arguments": {"label": "test", "hostname": "server.local", "workdir": "/scratch"}
            },
        )
        assert direct.status_code == 404
        assert direct.json()["error"]["code"] == "tool_not_found"


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
