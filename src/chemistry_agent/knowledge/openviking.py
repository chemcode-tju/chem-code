"""OpenViking HTTP API adapter. Research facts keep their platform provenance."""

import hashlib
import json
from pathlib import PurePosixPath

import httpx

from chemistry_agent.errors import AgentError
from chemistry_agent.knowledge.config import KnowledgeConfig
from chemistry_agent.memory.scope import MemoryScope


def scope_path(scope: MemoryScope) -> str:
    owner = hashlib.sha256((scope.owner_id or "local").encode()).hexdigest()[:24]
    project = hashlib.sha256((scope.project_id or "personal").encode()).hexdigest()[:24]
    return f"users/{owner}/projects/{project}"


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or any(p in {"..", "archive"} for p in path.parts)
        or any(c in value for c in "%?#\\:")
    ):
        raise AgentError("knowledge_path_invalid", "请使用知识范围内的相对路径。", 422)
    return str(path)


class OpenViking:
    """No hidden chat upload or automatic extraction of unverified scientific facts."""

    def __init__(self, config: KnowledgeConfig, transport=None) -> None:
        self.config, self.transport = config, transport

    async def request(self, method: str, path: str, **kwargs):
        config = self.config.read()
        if not config.enabled:
            raise AgentError("knowledge_disabled", "请先在知识与技能中连接 OpenViking。", 409)
        headers = {"X-API-Key": config.api_key.get_secret_value()} if config.api_key else {}
        try:
            async with httpx.AsyncClient(
                base_url=config.endpoint,
                headers=headers,
                timeout=30,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, path, **kwargs)
            if method == "DELETE" and response.status_code == 404:
                return {"deleted": True, "already_missing": True}
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "error" or data.get("error"):
                raise AgentError("openviking_error", "OpenViking 未完成操作，请查看服务日志。", 502)
            return data.get("result", data)
        except (httpx.HTTPError, ValueError) as exc:
            # Do not echo URLs, HTTP headers or provider error bodies containing credentials.
            raise AgentError(
                "openviking_unavailable", "OpenViking 连接失败，请检查服务与授权。", 503
            ) from exc

    def root(self, scope: MemoryScope) -> str:
        return f"{self.config.read().root_uri}/{scope_path(scope)}"

    def uri(self, scope: MemoryScope, path: str) -> str:
        return f"{self.root(scope)}/{relative_path(path)}"

    async def search(self, scope: MemoryScope, query: str, limit: int = 6) -> dict:
        root = self.root(scope)
        result = await self.request(
            "POST",
            "/api/v1/search/find",
            json={
                "query": query,
                "target_uri": root,
                "limit": limit,
            },
        )
        candidates = (
            result
            if isinstance(result, list)
            else [
                hit
                for group in ("resources", "memories", "skills")
                for hit in result.get(group, [])
            ]
        )
        items = []
        for hit in candidates:
            uri = hit.get("uri", "")
            # Home aliases can be expanded by the server to a canonical user URI.
            canonical_path = uri.removeprefix("viking://user/").partition("/")[2]
            expected = root.removeprefix("viking://~/") + "/"
            relative = None
            if uri.startswith(root + "/"):
                relative = uri[len(root) + 1 :]
            elif (
                root.startswith("viking://~/")
                and uri.startswith("viking://user/")
                and canonical_path.startswith(expected)
            ):
                relative = canonical_path[len(expected) :]
            if relative:
                try:
                    relative = relative_path(relative)
                except AgentError:
                    continue
                items.append(
                    {
                        "path": relative,
                        "abstract": hit.get("abstract", ""),
                        "score": hit.get("score"),
                        "source": "openviking",
                    }
                )
        return {"items": items, "scope": scope_path(scope)}

    async def read(self, scope: MemoryScope, path: str, level: str = "content") -> dict:
        operation = {"content": "read", "abstract": "abstract", "overview": "overview"}[level]
        result = await self.request(
            "GET", f"/api/v1/content/{operation}", params={"uri": self.uri(scope, path)}
        )
        return {"path": path, "level": level, "content": result}

    async def write(self, scope: MemoryScope, path: str, content: str) -> dict:
        uri = self.uri(scope, path)
        result = await self.request(
            "POST",
            "/api/v1/content/write",
            json={
                "uri": uri,
                "content": content,
                "mode": "replace",
                "wait": True,
            },
        )
        return {"path": path, "result": result}

    async def delete(self, scope: MemoryScope, path: str) -> None:
        await self.request("DELETE", "/api/v1/fs", params={"uri": self.uri(scope, path)})

    async def probe(self) -> dict:
        result = await self.request("GET", "/api/v1/system/status")
        return {"connected": True, "service": "OpenViking", "status": result}


def evidence_document(row: dict) -> str:
    body = json.loads(row["document"])
    metadata = {k: v for k, v in body.items() if k != "content"}
    metadata.update(
        memory_id=row["id"],
        version=row["version"],
        digest=row["digest"],
        source_session_id=row["session_id"],
    )
    return (
        f"# {body['title']}\n\n{body['content']}\n\n```json\n"
        + json.dumps(metadata, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
