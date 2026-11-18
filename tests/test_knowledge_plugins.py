"""Real skill packages and API boundaries, without LLMs or third-party credentials."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.errors import AgentError
from chemistry_agent.knowledge.config import KnowledgeConfig, KnowledgeQuery, KnowledgeUpdate
from chemistry_agent.knowledge.openviking import OpenViking
from chemistry_agent.knowledge.plugins import PluginInstall, SkillPlugins, SkillRead
from chemistry_agent.knowledge.service import Knowledge
from chemistry_agent.memory.models import MemoryDocument
from chemistry_agent.memory.scope import MemoryScope
from chemistry_agent.memory.service import ResearchMemory
from chemistry_agent.store import Store


def make_skill(root: Path, name: str = "research-skill") -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Inspect computational research data\n---\n"
        "Use scripts/read.py to inspect data.\n"
    )
    (directory / "scripts").mkdir()
    (directory / "scripts/read.py").write_text("print('portable-skill')\n")
    (root / "LICENSE").write_text("Test fixture license")
    return directory


async def test_marketplace_pin_restart_read_and_materialize(tmp_path):
    source = tmp_path / "source"
    directory = make_skill(source)
    (directory / "outside").symlink_to(tmp_path)
    (source / ".claude-plugin").mkdir()
    (source / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "science", "source": "./", "skills": ["./skills/research-skill"]}
                ]
            }
        )
    )
    plugins = SkillPlugins(tmp_path / "platform")
    request = PluginInstall(source=str(source), plugin="science")
    package = await plugins.install(request)
    assert (await plugins.install(request))["id"] == package["id"]
    plugins = SkillPlugins(tmp_path / "platform")
    entry = plugins.catalog()["skills"][0]
    result = plugins.read(SkillRead(skill=entry["id"]))
    assert result["revision"] == package["revision"]
    assert "scripts/read.py" in result["files"]
    assert not any("archive" in p or "outside" in p for p in result["files"])
    target = plugins.materialize(entry["id"], tmp_path / "agent")
    assert (Path(target["directory"]) / "scripts/read.py").is_file()
    (Path(target["directory"]) / "scripts/read.py").write_text("print('working-edit')")
    plugins.materialize(entry["id"], tmp_path / "agent")
    assert "working-edit" in (Path(target["directory"]) / "scripts/read.py").read_text()
    assert (plugins.root / package["id"] / "LICENSE").is_file()
    plugins.enable(package["id"], False)
    assert not plugins.catalog()["skills"]
    with pytest.raises(AgentError):
        plugins.read(SkillRead(skill=entry["id"]))


async def test_plugin_update_preserves_versions_and_rejects_traversal(tmp_path):
    source = tmp_path / "source"
    skill = make_skill(source)
    plugins = SkillPlugins(tmp_path / "platform")
    old = await plugins.install(PluginInstall(source=str(source)))
    (skill / "scripts/read.py").write_text("print('new-version')")
    new = await plugins.install(PluginInstall(source=str(source)))
    assert new["id"] != old["id"]
    assert len(plugins.packages()) == 2
    assert not plugins.packages()[0]["enabled"]
    with pytest.raises(AgentError):
        plugins.read(SkillRead(skill=plugins.catalog()["skills"][0]["id"], path="../../LICENSE"))
    plugins.enable(old["id"], True)
    assert [p["id"] for p in plugins.packages() if p["enabled"]] == [old["id"]]


async def test_openviking_identity_search_and_secrets(tmp_path):
    config = KnowledgeConfig(tmp_path)
    config.save(KnowledgeUpdate(enabled=True, api_key="private-test-key"))
    scope = MemoryScope("user-a", "project-a")
    other = MemoryScope("user-b", "project-a")
    seen = []
    client = None

    def respond(request):
        seen.append(request)
        assert request.headers["X-API-Key"] == "private-test-key"
        if request.url.path.endswith("find"):
            assert json.loads(request.content)["target_uri"] == client.root(scope)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "result": {
                        "resources": [
                            {
                                "uri": client.uri(scope, "notes/cu.md"),
                                "abstract": "Cu",
                                "score": 0.9,
                            },
                            {"uri": client.uri(other, "notes/private.md"), "abstract": "excluded"},
                            {
                                "uri": client.uri(scope, "notes/canonical.md").replace(
                                    "viking://~/", "viking://user/alice/"
                                ),
                                "abstract": "canonical",
                            },
                            {
                                "uri": client.uri(scope, "notes/unrelated.md").replace(
                                    "viking://~/resources/chem-code/",
                                    "viking://user/alice/unrelated/",
                                ),
                                "abstract": "excluded root",
                            },
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"status": "ok", "result": "content"})

    client = OpenViking(config, httpx.MockTransport(respond))
    result = await client.search(scope, "Cu surface")
    assert [item["path"] for item in result["items"]] == ["notes/cu.md", "notes/canonical.md"]
    await client.write(scope, "notes/cu.md", "source: calculation-42")
    assert json.loads(seen[-1].content)["mode"] == "replace"
    assert (await client.read(scope, "notes/cu.md"))["content"] == "content"
    assert "private-test-key" not in json.dumps(config.public())
    config.save(KnowledgeUpdate(enabled=True))
    assert config.public()["has_api_key"]
    config.save(KnowledgeUpdate(enabled=True, endpoint="http://another-service"))
    assert not config.public()["has_api_key"]
    for path in ("../other.md", "/root.md", "archive/x.md", "%2e%2e/x.md"):
        with pytest.raises(AgentError):
            client.uri(scope, path)


async def test_personal_knowledge_crosses_projects_and_preserves_evidence_namespace(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    documents = {}

    def respond(request):
        if request.url.path.endswith("write"):
            body = json.loads(request.content)
            documents[body["uri"]] = body["content"]
            return httpx.Response(200, json={"result": {}})
        target = json.loads(request.content)["target_uri"]
        return httpx.Response(
            200,
            json={
                "result": {
                    "resources": [
                        {"uri": uri, "abstract": content}
                        for uri, content in documents.items()
                        if uri.startswith(target + "/")
                    ]
                }
            },
        )

    try:
        service = Knowledge(store, tmp_path, tmp_path / "workspaces", httpx.MockTransport(respond))
        service.config.save(KnowledgeUpdate(enabled=True))
        project_a, project_b = MemoryScope("owner", "a"), MemoryScope("owner", "b")
        await service.write(service.scope(project_a, "personal"), "preferences.md", "Use ASE")
        await service.write(project_a, "parameters.md", "Cu reference")
        query = KnowledgeQuery(query="Cu")
        assert len((await service.search(project_a, query))["items"]) == 2
        result = (await service.search(project_b, query))["items"]
        assert len(result) == 1 and result[0]["scope"] == "personal"
        assert not (await service.search(MemoryScope("other", "a"), query))["items"]
        for path in ("research/a.md", "./research/a.md", "research//a.md"):
            with pytest.raises(AgentError, match="memory_save"):
                await service.write(project_a, path, "cannot replace evidence")
    finally:
        await store.close()


async def test_sync_restarts_and_failure_preserves_source(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    writes = []
    failed = True

    def respond(request):
        if failed:
            return httpx.Response(503, text="provider unavailable")
        writes.append(json.loads(request.content) if request.content else {"delete": True})
        return httpx.Response(200, json={"status": "ok", "result": {}})

    def service():
        return Knowledge(store, tmp_path, tmp_path / "workspaces", httpx.MockTransport(respond))

    scope = MemoryScope("owner", "project")
    try:
        knowledge = service()
        knowledge.config.save(KnowledgeUpdate(enabled=True))
        await knowledge.sync.initialize()
        record = await ResearchMemory(store).save(
            scope,
            MemoryDocument(
                title="Cu lattice",
                kind="lesson",
                content="Read the source output before reuse",
                systems=["Cu"],
                tasks=["bulk"],
                parameters={"ENCUT": 400},
            ),
            source_key="calc:42",
        )
        with pytest.raises(AgentError):
            await knowledge.sync.sync()
        assert (await ResearchMemory(store).read(scope, record["id"]))["parameters"]["ENCUT"] == 400
        failed = False
        assert (await knowledge.sync.sync())["updated"] == 1
        assert (await service().sync.sync())["updated"] == 0
        assert record["id"] in writes[0]["content"]
        await ResearchMemory(store).forget(scope, record["id"], record["version"])
        assert (await knowledge.sync.sync())["updated"] == 1
        with pytest.raises(AgentError):
            await knowledge.read(scope, f"research/{record['id']}.md")
    finally:
        await store.close()


def test_settings_and_external_mcp_do_not_need_native_session(settings_factory, tmp_path):
    settings = settings_factory()
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 20000)
    ) as client:
        body = {"enabled": True, "api_key": "secret-value"}
        assert client.put("/api/knowledge/settings", json=body).status_code == 200
        assert "secret-value" not in client.get("/api/knowledge/settings").text
        source = tmp_path / "source"
        make_skill(source)
        assert (
            client.post("/api/knowledge/plugins", json={"source": str(source)}).status_code == 200
        )
        tools = client.post("/api/knowledge/mcp", json={"id": 1, "method": "tools/list"}).json()
        names = {tool["name"] for tool in tools["result"]["tools"]}
        assert {"memory_read", "skill_list", "knowledge_search"} <= names
        data = client.post(
            "/api/knowledge/mcp",
            json={
                "id": 2,
                "method": "tools/call",
                "params": {"name": "skill_list", "arguments": {}},
            },
        ).json()
        assert "research-skill" in data["result"]["content"][0]["text"]
        assert (
            client.post(
                "/api/knowledge/mcp", json={"method": "notifications/initialized"}
            ).status_code
            == 202
        )
        forbidden = client.put(
            "/api/knowledge/settings", json=body, headers={"Origin": "https://unrelated.example"}
        )
        assert forbidden.status_code == 403
