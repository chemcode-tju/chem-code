"""SSH channels share one authenticated connection for a native turn."""

import asyncio
import json
import shlex
from contextlib import asynccontextmanager
from pathlib import Path

from chemistry_agent.automation.ssh_profiles import load_profile, shell_profiles
from chemistry_agent.errors import AgentError
from chemistry_agent.hosting import remote_helper

HELPER = Path(remote_helper.__file__).read_text()


@asynccontextmanager
async def connection(hosting, host: str):
    try:
        import asyncssh
    except ImportError as exc:
        raise AgentError(
            "ssh_dependency_missing", "远端 Agent 需要安装 automation 依赖中的 asyncssh。", 503
        ) from exc
    row = hosting.host(host)
    alias = row.get("ssh_alias") or row["id"]
    profiles = shell_profiles(hosting.environments.platform_root)
    try:
        if alias in profiles:
            from chemistry_agent.automation.ssh_connection import connect

            ssh = await connect(
                hosting.environments.platform_root,
                load_profile(hosting.environments.platform_root, profiles[alias]),
            )
        else:
            ssh = await asyncssh.connect(
                alias,
                config=(),
                agent_forwarding=False,
                connect_timeout=12,
                login_timeout=15,
                keepalive_interval=20,
                keepalive_count_max=3,
            )
        async with ssh:
            yield ssh
    except (OSError, asyncssh.Error) as exc:
        raise AgentError("agent_host_connection", f"主机 {host} 连接中断：{exc}", 503) from exc


def helper_command() -> str:
    # A login shell discovers the host's own PATH, not the platform's executable paths.
    return "bash -lc " + shlex.quote("python3 -u -c " + shlex.quote(HELPER))


async def helper(ssh, payload: dict) -> dict:
    async with asyncio.timeout(30):
        result = await ssh.run(helper_command(), input=json.dumps(payload) + "\n", check=False)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise AgentError(
            "agent_host_protocol", "远端 Python 启动失败或登录脚本污染了标准输出。", 502
        ) from exc
    if data.get("error") or result.exit_status:
        raise AgentError("agent_host_prepare", data.get("error") or result.stderr[-1000:], 502)
    return data


async def discover(hosting, host: str) -> dict:
    hosting.host(host)
    if host == "local":
        return await asyncio.to_thread(remote_helper.discover)
    async with connection(hosting, host) as ssh:
        return await helper(ssh, {"operation": "discover"})
