"""Store only SSH passwords in the login Keychain of the macOS service user."""

import sys

from chemistry_agent.errors import AgentError

SERVICE = "Chemistry Agent SSH"


def _keychain():
    if sys.platform != "darwin":
        raise AgentError(
            "keychain_unavailable", "密码模式需要运行服务的 Mac 钥匙串；也可选择密钥。", 422
        )
    try:
        from keyring.backends.macOS import Keyring

        return Keyring()
    except (ImportError, RuntimeError) as exc:
        raise AgentError(
            "keychain_unavailable", "请在运行服务的环境中安装 keyring 并解锁登录钥匙串。", 503
        ) from exc


def load_password(identifier: str) -> str:
    try:
        value = _keychain().get_password(SERVICE, identifier)
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError(
            "keychain_locked", "无法读取钥匙串。请解锁运行服务账户的登录钥匙串。", 403
        ) from exc
    if value is None:
        raise AgentError("ssh_password_missing", "请在设置中为这个服务器保存 SSH 密码。", 422)
    return value


def set_password(identifier: str, value: str | None) -> None:
    backend = _keychain()
    try:
        if value is None:
            if backend.get_password(SERVICE, identifier) is not None:
                backend.delete_password(SERVICE, identifier)
        else:
            backend.set_password(SERVICE, identifier, value)
    except Exception as exc:
        raise AgentError(
            "keychain_write_failed", "无法更新登录钥匙串。请检查服务账户及钥匙串权限。", 403
        ) from exc
