"""Probe the server's public identity before using any credential. Pin it for every client."""

import asyncio
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import asyncssh
from filelock import FileLock

from chemistry_agent.automation.models import ServerCreate
from chemistry_agent.automation.ssh_keychain import load_password
from chemistry_agent.automation.ssh_profiles import pin_path, write_private
from chemistry_agent.errors import AgentError


@dataclass(frozen=True)
class Target:
    hostname: str
    port: int
    username: str
    files: tuple[Path, ...]
    lookup: str


def resolve(server: ServerCreate) -> Target:
    files = (Path.home() / ".ssh/known_hosts", Path("/etc/ssh/ssh_known_hosts"))
    if server.auth_mode != "alias":
        return Target(server.hostname, server.port or 22, server.username, files, server.hostname)
    try:
        raw = subprocess.run(
            ["ssh", "-G", "--", server.hostname],
            capture_output=True,
            text=True,
            timeout=6,
            check=True,
        )
        config = dict(line.split(" ", 1) for line in raw.stdout.splitlines() if " " in line)
        hostname = config["hostname"]
        paths = " ".join(config.get(k, "") for k in ("userknownhostsfile", "globalknownhostsfile"))
        files = tuple(Path(p).expanduser() for p in paths.split() if p not in {"none", "/dev/null"})
        return Target(
            hostname,
            int(config["port"]),
            config["user"],
            files,
            config.get("hostkeyalias") or hostname,
        )
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        raise AgentError(
            "ssh_config_invalid", "OpenSSH 无法解析该别名，请检查 ~/.ssh/config。", 422
        ) from exc


def known_keys(target: Target) -> tuple[list, list]:
    trusted, revoked = [], []
    for path in target.files:
        if path.is_file():
            try:
                lookup = target.lookup if target.port == 22 else f"[{target.lookup}]:{target.port}"
                match = asyncssh.read_known_hosts(str(path)).match(lookup, "", None)
            except (OSError, ValueError) as exc:
                raise AgentError(
                    "known_hosts_invalid", "无法读取本机 SSH 主机信任记录。", 422
                ) from exc
            trusted.extend(match[0])
            revoked.extend(match[2])
    return trusted, revoked


def destination(server: ServerCreate, target: Target) -> dict:
    return {
        "host": server.hostname,
        "port": target.port,
        "config": () if server.auth_mode == "alias" else None,
    }


async def discover(root: Path, server: ServerCreate) -> tuple[Target, str, dict]:
    target = await asyncio.to_thread(resolve, server)
    path = pin_path(root, target.hostname, target.port)
    previous = json.loads(path.read_text()) if path.exists() else None
    trusted, revoked = known_keys(target)
    try:
        async with asyncio.timeout(10):
            options = destination(server, target)
            expected = [asyncssh.import_public_key(previous["key"])] if previous else trusted
            algorithms = list(
                dict.fromkeys(alg.decode() for key in expected for alg in key.sig_algorithms)
            )
            if algorithms:
                try:
                    key = await asyncssh.get_server_host_key(
                        **options, server_host_key_algs=algorithms
                    )
                except asyncssh.KeyExchangeFailed:
                    key = await asyncssh.get_server_host_key(**options)
            else:
                key = await asyncssh.get_server_host_key(**options)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise AgentError(
            "ssh_probe_failed", "无法取得主机指纹，请检查地址、端口及跳板连接。", 422
        ) from exc
    if key is None or key in revoked:
        raise AgentError("ssh_host_revoked", "服务器没有可验证的主机密钥或密钥已被撤销。", 403)
    fingerprint = key.get_fingerprint()
    system_conflict = bool(trusted and key not in trusted)
    matches = (
        fingerprint == previous["fingerprint"] if previous else key in trusted
    ) and not system_conflict
    result = {
        "hostname": target.hostname,
        "port": target.port,
        "username": target.username,
        "fingerprint": fingerprint,
        "algorithm": key.get_algorithm(),
        "trusted": matches,
        "changed": bool((previous or trusted) and not matches),
        "previous_fingerprint": previous["fingerprint"]
        if previous and not system_conflict
        else None,
    }
    return target, key.export_public_key().decode().strip(), result


async def pin_server(
    root: Path,
    server: ServerCreate,
    fingerprint: str | None = None,
    previous_fingerprint: str | None = None,
) -> tuple[Target, dict]:
    target, public, result = await discover(root, server)
    if fingerprint is not None and result["fingerprint"] != fingerprint:
        raise AgentError("ssh_fingerprint_changed", "主机指纹与刚才查看的不一致；请重新核对。", 409)
    if result["changed"] and (
        not previous_fingerprint or previous_fingerprint != result["previous_fingerprint"]
    ):
        raise AgentError(
            "ssh_host_changed", "主机密钥已改变。请和管理员核对；本机已有记录须先由用户处理。", 409
        )
    if not result["trusted"] and not fingerprint:
        raise AgentError(
            "ssh_trust_required", "请先在设置中独立核对并信任服务器 SHA256 指纹。", 409
        )
    path = pin_path(root, target.hostname, target.port)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(path) + ".lock"):
        current = json.loads(path.read_text()) if path.exists() else None
        if current and current["fingerprint"] not in {result["fingerprint"], previous_fingerprint}:
            raise AgentError("ssh_host_changed", "保存期间主机信任记录改变，请重新探测。", 409)
        write_private(path, {"key": public, "fingerprint": result["fingerprint"]})
    return target, {**result, "trusted": True, "changed": False}


def identity_path(reference: str) -> str:
    identity = Path(reference).expanduser().resolve()
    if not identity.is_relative_to((Path.home() / ".ssh").resolve()) or not identity.is_file():
        raise AgentError("ssh_identity_invalid", "未找到指定的本机私钥。", 422)
    if stat.S_IMODE(identity.stat().st_mode) & 0o077:
        raise AgentError("ssh_identity_invalid", "私钥须仅本账户可读（chmod 600）。", 422)
    return str(identity)


async def connect(root: Path, profile: dict) -> asyncssh.SSHClientConnection:
    server = ServerCreate.model_validate(profile["server"])
    target = await asyncio.to_thread(resolve, server)
    if (target.hostname, target.port, target.username) != (
        profile["hostname"],
        profile["port"],
        profile["username"],
    ):
        raise AgentError("ssh_alias_changed", "SSH 别名的目标已改变，请重新注册并核对服务器。", 409)
    path = pin_path(root, target.hostname, target.port)
    if not path.exists():
        raise AgentError("ssh_trust_required", "缺少主机信任记录，请在设置中重新核实。", 403)
    key = asyncssh.import_public_key(json.loads(path.read_text())["key"])
    accepted, revoked = known_keys(target)
    if key in revoked:
        raise AgentError("ssh_host_revoked", "服务器主机密钥已被撤销。", 403)
    if accepted and key not in accepted:
        raise AgentError("ssh_host_changed", "绑定指纹与本机 known_hosts 不符，请先核对。", 403)
    options = destination(server, target) | {
        "username": target.username,
        "known_hosts": ([key], [], []),
        "connect_timeout": 12,
        "login_timeout": 15,
        "agent_forwarding": False,
    }
    if server.auth_mode == "password":
        options.update(
            password=await asyncio.to_thread(load_password, profile["id"]),
            client_keys=None,
            agent_path=None,
            preferred_auth="password,keyboard-interactive",
        )
    elif server.identity_file:
        options["client_keys"] = [await asyncio.to_thread(identity_path, server.identity_file)]
    elif server.auth_mode == "key":
        options.update(client_keys=[])
    try:
        return await asyncssh.connect(**options)
    except asyncssh.HostKeyNotVerifiable as exc:
        raise AgentError("ssh_host_changed", "主机密钥与已信任指纹不符。", 403) from exc
    except asyncssh.PermissionDenied as exc:
        raise AgentError(
            "ssh_login_denied", "SSH 认证被拒绝。请核对账号、钥匙串密码或代理中的密钥。", 403
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        raise AgentError(
            "ssh_connect_failed", "SSH 连接失败，请检查主机、端口和本机身份。", 422
        ) from exc
