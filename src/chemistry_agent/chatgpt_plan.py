"""Independent Sign in with ChatGPT registration for the local Chemistry Agent host."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from chemistry_agent.errors import AgentError
from chemistry_agent.model_settings import ModelSettingsStore

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "Chemistry Agent"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"


def require(condition: Any, message: str) -> None:
    if not condition:
        raise AgentError("chatgpt_auth_invalid", message, 502)


def auth_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    require(
        parsed.scheme == "https" and parsed.netloc == "auth.openai.com" and not parsed.fragment,
        "OpenAI 授权地址无效。",
    )
    return url


def verify_identity(
    token: str, keys: dict, client_id: str, *, nonce: str | None = None, subject: str | None = None
) -> dict:
    """Validate signatures and binding; decoding a JWT alone does not establish identity."""
    try:
        header = jwt.get_unverified_header(token)
        require(
            header.get("alg") in {"RS256", "ES256"} and not header.get("crit"), "不支持的身份签名。"
        )
        key = next(
            item
            for item in keys["keys"]
            if item.get("kid") == header.get("kid")
            and item.get("use", "sig") == "sig"
            and item.get("alg", header["alg"]) == header["alg"]
        )
        signing_key = jwt.PyJWK.from_dict(key, algorithm=header["alg"])
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[header["alg"]],
            audience=client_id,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
        audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        require(
            (len(audience) == 1 and not claims.get("azp")) or claims.get("azp") == client_id,
            "身份令牌的客户端不匹配。",
        )
        require(bool(claims["sub"]), "身份令牌缺少账号标识。")
        require(nonce is None or claims.get("nonce") == nonce, "身份令牌的 nonce 不匹配。")
        require(subject is None or claims["sub"] == subject, "授权账号发生变化，请添加其他账号。")
        return claims
    except (jwt.PyJWTError, KeyError, ValueError, TypeError, StopIteration) as exc:
        raise AgentError(
            "chatgpt_identity_invalid", "无法验证 OpenAI 账号身份，请重新授权。", 502
        ) from exc


def authorization_url(
    record: dict, host_id: str, redirect_uri: str, state: str, nonce: str, verifier: str
) -> str:
    parameters = {
        "client_id": record.get("client_id", "dynamic_agent_client"),
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("="),
    }
    if not record.get("client_id"):
        parameters["agent_name_hint"] = APP_NAME
    # Hints deliberately omitted: no ID token passes through the web UI or status endpoint.
    return f"{ISSUER}/api/accounts/authorize?{urlencode(parameters)}"


def token_record(data: dict, previous: dict, identity: dict | None = None) -> dict:
    require(
        isinstance(data.get("access_token"), str)
        and data["access_token"]
        and str(data.get("token_type", "")).lower() == "bearer",
        "授权令牌无效。",
    )
    expiry = data.get("expires_in")
    require(isinstance(expiry, (int, float)) and 0 < expiry < 365 * 86400, "授权有效期无效。")
    scopes = (
        data["scope"].split() if isinstance(data.get("scope"), str) else previous.get("scopes", [])
    )
    require(PLAN_SCOPE in scopes, "未授权 ChatGPT 套餐用量，请在官方页面启用后重新授权。")
    return previous | {
        "subject": identity["sub"] if identity else previous.get("subject"),
        "email": identity.get("email", "") if identity else previous.get("email", ""),
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", previous.get("refresh_token")),
        "id_token": data.get("id_token", previous.get("id_token")),
        "scopes": scopes,
        "expires_at": time.time() + expiry,
    }


class ChatGPTPlan:
    def __init__(
        self, store: ModelSettingsStore, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.store = store
        self.transport = transport

    def path(self, user_id: str | None) -> Path:
        return self.store.directory(user_id) / "chatgpt-plan.json"

    def load(self, user_id: str | None) -> dict:
        try:
            return json.loads(self.path(user_id).read_text())
        except FileNotFoundError:
            return {"active": None, "selected": False, "accounts": []}
        except (ValueError, OSError) as exc:
            raise AgentError("chatgpt_store_invalid", "ChatGPT 授权记录无法读取。", 503) from exc

    def save(self, user_id: str | None, data: dict) -> None:
        path = self.path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as handle:
                json.dump(data, handle)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @asynccontextmanager
    async def locked(self, user_id: str | None):
        """Serialize rotating refresh tokens across tasks and server processes."""
        directory = self.path(user_id).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = FileLock(directory / "chatgpt-plan.lock", thread_local=False)
        acquired = False
        try:
            for _ in range(300):
                try:
                    lock.acquire(timeout=0)
                    acquired = True
                    break
                except FileLockTimeout:
                    await asyncio.sleep(0.1)
            require(acquired, "另一个授权操作尚未结束，请稍后重试。")
            yield
        finally:
            if acquired:
                lock.release()

    def active(self, user_id: str | None) -> dict:
        data = self.load(user_id)
        return next((item for item in data["accounts"] if item["client_id"] == data["active"]), {})

    def selected(self, user_id: str | None) -> bool:
        return bool(self.load(user_id).get("selected"))

    def authenticated(self, user_id: str | None) -> bool:
        record = self.active(user_id)
        return bool(
            record.get("access_token")
            and PLAN_SCOPE in record.get("scopes", [])
            and (record.get("refresh_token") or record.get("expires_at", 0) > time.time())
        )

    def public(self, user_id: str | None) -> dict:
        data = self.load(user_id)
        return {
            "active_account": data["active"] if data.get("selected") else None,
            "accounts": [
                {
                    "id": item["client_id"],
                    "label": f"{item.get('email') or 'ChatGPT'} · {item['client_id'][-8:]}",
                    "authenticated": bool(item.get("access_token")),
                }
                for item in data["accounts"]
            ],
        }

    async def request(self, url: str, **options: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=self.transport, timeout=30, follow_redirects=False
        ) as client:
            try:
                response = await client.request(
                    options.pop("method", "GET"), auth_endpoint(url), **options
                )
            except httpx.HTTPError as exc:
                raise AgentError(
                    "chatgpt_auth_network", "无法连接 OpenAI 授权服务，请稍后重试。", 502
                ) from exc
        if not response.is_success:
            code = "chatgpt_auth_failed"
            try:
                if response.json().get("error") == "invalid_grant":
                    code = "chatgpt_reauthorize"
            except (ValueError, AttributeError):
                pass
            raise AgentError(
                code, f"OpenAI 授权失败（HTTP {response.status_code}），请重试或重新授权。", 502
            )
        return response

    async def discovery(self) -> dict:
        data = (await self.request(f"{ISSUER}/.well-known/openid-configuration")).json()
        require(data.get("issuer") == ISSUER, "OpenAI 身份服务不匹配。")
        for name in ("token_endpoint", "jwks_uri", "revocation_endpoint"):
            auth_endpoint(data[name])
        return data

    async def identity(self, metadata: dict, token: str, client_id: str, **binding: Any) -> dict:
        keys = (await self.request(metadata["jwks_uri"])).json()
        return verify_identity(token, keys, client_id, **binding)

    async def select_account(self, user_id: str | None, account_id: str) -> None:
        async with self.locked(user_id):
            data = self.load(user_id)
            require(
                any(item["client_id"] == account_id for item in data["accounts"]),
                "未知 ChatGPT 账号。",
            )
            record = next(item for item in data["accounts"] if item["client_id"] == account_id)
            require(
                record.get("subject")
                and record.get("access_token")
                and PLAN_SCOPE in record.get("scopes", []),
                "此账号需要先完成重新授权。",
            )
            data.update(active=account_id, selected=True)
            self.save(user_id, data)

    async def login(
        self,
        user_id: str | None,
        attempt: Any,
        *,
        account_id: str | None = None,
        new_account: bool = False,
    ) -> None:
        """Only a validated loopback attempt replaces active credentials."""
        async with self.locked(user_id):
            data = self.load(user_id)
            data.setdefault("host_id", f"urn:uuid:{uuid4()}")
            selected_id = (
                None
                if new_account
                else account_id or data["active"] or data.get("last_registration")
            )
            record = next(
                (item for item in data["accounts"] if item["client_id"] == selected_id), {}
            )
            require(not selected_id or record, "未知 ChatGPT 账号。")
            host_id = data["host_id"]
            self.save(user_id, data)
        metadata = await self.discovery()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        callback = asyncio.get_running_loop().create_future()
        connections: set[asyncio.Task] = set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            connections.add(task)
            status, message = "400 Bad Request", "Invalid authorization callback."
            try:
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                method, target, _ = headers.decode("ascii").split("\r\n", 1)[0].split(" ")
                parsed = urlsplit(target)
                query = parse_qs(parsed.query, keep_blank_values=True)
                valid_state = query.get("state") == [state]
                if (
                    method == "GET"
                    and parsed.path == "/auth/callback"
                    and valid_state
                    and not callback.done()
                ):
                    if any(len(values) != 1 for values in query.values()):
                        raise ValueError("Duplicate OAuth parameter")
                    callback.set_result({key: values[0] for key, values in query.items()})
                    status = "200 OK"
                    message = (
                        "Return to Chemistry Agent to check the authorization result. "
                        "You may close this tab."
                    )
                body = message.encode()
                writer.write(
                    (
                        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                        "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
                    ).encode()
                    + body
                )
                await writer.drain()
            except (
                ValueError,
                OSError,
                TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                connections.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=16384)
        try:
            redirect_uri = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback"
            attempt.url = authorization_url(record, host_id, redirect_uri, state, nonce, verifier)
            attempt.state = "waiting"
            result = await asyncio.wait_for(callback, 600)
            require(not result.get("error"), "ChatGPT 授权被取消或拒绝。")
            issued_id = result.get("client_id") or record.get("client_id")
            require(
                isinstance(issued_id, str) and issued_id and issued_id != "dynamic_agent_client",
                "OpenAI 未返回独立客户端 ID。",
            )
            require(not record or issued_id == record["client_id"], "授权的客户端发生变化。")
            require(result.get("code"), "授权回调缺少 code。")
            async with self.locked(user_id):
                data = self.load(user_id)
                if not record:
                    require(
                        not any(item["client_id"] == issued_id for item in data["accounts"]),
                        "此账号已注册，请从账号列表重新授权。",
                    )
                    record = {"client_id": issued_id}
                    data["accounts"].append(record)
                    # Keep issued IDs even when code exchange fails; do not register duplicates.
                    data["last_registration"] = issued_id
                    self.save(user_id, data)
                tokens = (
                    await self.request(
                        metadata["token_endpoint"],
                        method="POST",
                        data={
                            "grant_type": "authorization_code",
                            "client_id": issued_id,
                            "code": result["code"],
                            "code_verifier": verifier,
                            "redirect_uri": redirect_uri,
                            "resource": RESOURCE,
                        },
                    )
                ).json()
                identity = await self.identity(
                    metadata,
                    tokens.get("id_token"),
                    issued_id,
                    nonce=nonce,
                    subject=record.get("subject"),
                )
                fresh = token_record(tokens, {"client_id": issued_id}, identity)
                data["accounts"] = [
                    fresh if item["client_id"] == issued_id else item for item in data["accounts"]
                ]
                data.update(active=issued_id, selected=True)
                self.save(user_id, data)
            attempt.state = "complete"
        finally:
            server.close()
            await server.wait_closed()
            for task in list(connections):
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)

    @staticmethod
    def clear_tokens(record: dict) -> None:
        for key in ("access_token", "refresh_token", "id_token", "scopes", "expires_at"):
            record.pop(key, None)

    async def access_token(self, user_id: str | None) -> str:
        async with self.locked(user_id):
            data = self.load(user_id)
            record = next(
                (item for item in data["accounts"] if item["client_id"] == data["active"]), {}
            )
            require(
                record.get("access_token") and PLAN_SCOPE in record.get("scopes", []),
                "请先使用 ChatGPT 授权 Chemistry Agent。",
            )
            if record.get("expires_at", 0) > time.time() + 120:
                return record["access_token"]
            require(record.get("refresh_token"), "ChatGPT 登录已过期，请重新授权。")
            metadata = await self.discovery()
            try:
                tokens = (
                    await self.request(
                        metadata["token_endpoint"],
                        method="POST",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": record["client_id"],
                            "refresh_token": record["refresh_token"],
                            "resource": RESOURCE,
                        },
                    )
                ).json()
                identity = (
                    await self.identity(
                        metadata, tokens["id_token"], record["client_id"], subject=record["subject"]
                    )
                    if tokens.get("id_token")
                    else None
                )
                record.update(token_record(tokens, record, identity))
                self.save(user_id, data)
                return record["access_token"]
            except AgentError as exc:
                if exc.code == "chatgpt_reauthorize":
                    self.clear_tokens(record)
                    self.save(user_id, data)
                raise

    async def logout(self, user_id: str | None) -> dict:
        async with self.locked(user_id):
            data = self.load(user_id)
            record = next(
                (item for item in data["accounts"] if item["client_id"] == data["active"]), {}
            )
            revoked = not record.get("refresh_token")
            if not revoked:
                for retry in range(3):
                    try:
                        metadata = await self.discovery()
                        response = await self.request(
                            metadata["revocation_endpoint"],
                            method="POST",
                            data={
                                "token": record["refresh_token"],
                                "token_type_hint": "refresh_token",
                                "client_id": record["client_id"],
                            },
                        )
                        require(response.status_code == 200, "远端撤销未确认。")
                        revoked = True
                        break
                    except (AgentError, ValueError, KeyError):
                        if retry < 2:
                            await asyncio.sleep(0.5 * 2**retry)
            self.clear_tokens(record)
            data["selected"] = True
            self.save(user_id, data)
            return {"remote_revoked": revoked}
