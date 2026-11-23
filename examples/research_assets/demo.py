"""Run the synthetic two-lab research asset demonstration."""

import base64
import hashlib
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from catalog import AssetUnavailable, Catalog
from fastapi import FastAPI
from fixture import CONSUMER, METHOD_KEY, PRODUCER, RECIPE_V1, load_fixture, register_summary
from server import create_app

# Configuration
serve_catalog = False
listen_port = 8879


class AgentClient:
    """Use the same read-only MCP tools from separate Agent identities."""

    def __init__(self, http: httpx.Client, token: str, agent: str) -> None:
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        self.agent = agent

    def request(self, method: str, params: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        response = self.http.post("/mcp", json=message, headers=self.headers)
        response.raise_for_status()
        return response.json()["result"]

    def connect(self) -> None:
        metadata = self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": self.agent, "version": "1.0"},
            },
        )
        assert metadata["protocolVersion"] == "2025-11-25"
        initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        response = self.http.post("/mcp", json=initialized, headers=self.headers)
        assert response.status_code == 202
        assert {tool["name"] for tool in self.request("tools/list")["tools"]} == {
            "asset_search",
            "asset_inspect",
            "asset_fetch",
        }

    def call(self, tool: str, **arguments: object) -> dict:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert not result.get("isError"), result
        return result["structuredContent"]

    def unavailable(self, tool: str, **arguments: object) -> None:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        missing = self.request(
            "tools/call",
            {
                "name": tool,
                "arguments": arguments | {"revision": "0" * 64},
            },
        )
        assert result == missing and result.get("isError"), result

    def materialize(self, revision: str, name: str, target: Path) -> Path:
        manifest = self.call("asset_inspect", revision=revision)
        expected = next(entry["sha256"] for entry in manifest["files"] if entry["name"] == name)
        artifact = self.call("asset_fetch", revision=revision, name=name)
        data = base64.b64decode(artifact["content_base64"], validate=True)
        if hashlib.sha256(data).hexdigest() != expected or artifact["sha256"] != expected:
            raise ValueError("download differs from the pinned manifest")
        destination = target / name
        with destination.open("xb") as writer:
            writer.write(data)
        return destination


@contextmanager
def http_endpoint(app: FastAPI) -> Iterator[str]:
    """Run an actual loopback listener with an OS-allocated port."""
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    with socket.create_server(("127.0.0.1", 0)) as listener:
        host, port = listener.getsockname()
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise RuntimeError("demo HTTP listener did not start")
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def reuse_assets(
    catalog: Catalog, owner: AgentClient, consumer: AgentClient, refs: dict[str, str], root: Path
) -> str:
    filters = {
        "text": "Cu CO",
        "kind": "calculation",
        "formula": "CCuO",
        "functional": "PBE",
        "comparison_key": METHOD_KEY,
        "energy_metric": "TOTEN",
    }
    matches = consumer.call("asset_search", **filters)["hits"]
    assert [item["revision"] for item in matches] == [refs["calculation"]]
    assert consumer.call("asset_search", kind="calculation", functional="r2SCAN")["hits"]
    assert (
        consumer.call("asset_search", **(filters | {"energy_metric": "energy_without_entropy"}))[
            "hits"
        ]
        == []
    )
    unfiltered_review = owner.call("asset_search", **filters, approved_only=False)["hits"]
    assert {item["review_status"] for item in unfiltered_review} == {"draft", "approved"}
    try:
        catalog.approve(PRODUCER, refs["draft"], reviewer="surface-curator")
    except ValueError:
        pass
    else:
        raise AssertionError("scheduler completion must not pass scientific review")
    owner.unavailable("asset_fetch", revision=refs["draft"], name="CONTCAR")

    scripts = consumer.call("asset_search", kind="script", text="normalize")["hits"]
    assert [item["revision"] for item in scripts] == [refs["script_v1"]]
    assert refs["script_v2"] in {
        item["revision"] for item in owner.call("asset_search", kind="script")["hits"]
    }
    consumer.unavailable("asset_inspect", revision=refs["script_v2"])
    private_asset = consumer.request(
        "tools/call",
        {
            "name": "asset_search",
            "arguments": {"group": PRODUCER, "kind": "script"},
        },
    )
    assert private_asset.get("isError")
    print("检索命中 1 条 PBE/TOTEN 优化结果。口径不匹配或未复核的结果未进入默认复用列表。")
    print(f"合作组取得固定脚本 v1 {refs['script_v1'][:12]}，源组可查看自己的 v2。")

    workspace = root / "analysis-demo"
    workspace.mkdir()
    for name in ("CONTCAR", "energy.json"):
        consumer.materialize(refs["calculation"], name, workspace)
    script = consumer.materialize(refs["script_v1"], "summarize.py", workspace)
    if script.read_bytes() != RECIPE_V1:
        raise RuntimeError("the executable is not the bundled synthetic fixture")
    subprocess.run([sys.executable, "-I", str(script)], cwd=workspace, check=True, timeout=5)
    data = (workspace / "summary.json").read_bytes()
    assert json.loads(data) == {"metric": "TOTEN/N", "value": -5.37448559, "unit": "eV/atom"}
    inputs = (refs["calculation"], refs["script_v1"])
    revision = register_summary(catalog, inputs, data)
    report = consumer.call("asset_inspect", revision=revision)
    assert report["inputs"] == sorted(inputs)
    print(f"分析节点得到 {json.loads(data)['value']} eV/atom（虚构值）。")
    print(f"派生报告锁定结构与脚本的版本，报告 revision {revision}")
    return revision


def verify_sharing(
    catalog: Catalog,
    owner: AgentClient,
    consumer: AgentClient,
    refs: dict[str, str],
    report: str,
) -> None:
    third_group = "lab-third"
    saved_report = consumer.call("asset_fetch", revision=report, name="summary.json")
    try:
        catalog.share(CONSUMER, report, third_group)
    except AssetUnavailable:
        pass
    else:
        raise AssertionError("derived revisions require upstream access before publication")
    catalog.share(CONSUMER, report, PRODUCER)
    owner.call("asset_inspect", revision=report)

    catalog.revoke(PRODUCER, refs["calculation"], CONSUMER)
    consumer.unavailable("asset_fetch", revision=refs["calculation"], name="CONTCAR")
    consumer.unavailable("asset_inspect", revision=report)
    consumer.unavailable("asset_fetch", revision=report, name="summary.json")
    try:
        register_summary(
            catalog,
            (refs["calculation"], refs["script_v1"]),
            base64.b64decode(saved_report["content_base64"]),
        )
    except AssetUnavailable:
        pass
    else:
        raise AssertionError("a revoked upstream cannot be registered as an input")
    assert consumer.call("asset_search", kind="analysis")["hits"] == []
    assert (
        consumer.call(
            "asset_search",
            kind="calculation",
            comparison_key=METHOD_KEY,
        )["hits"]
        == []
    )
    owner.call("asset_inspect", revision=report)
    print("撤销上游共享后，合作组无法再次检索或读取原件及派生报告。源组的权限仍有效。")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="chem-research-assets-") as directory:
        root = Path(directory)
        database = root / "catalog.sqlite"
        catalog = Catalog(database)
        refs = load_fixture(catalog)
        tokens = {secrets.token_urlsafe(24): group for group in (PRODUCER, CONSUMER)}
        app = create_app(catalog, tokens)
        if serve_catalog:
            print(f"MCP http://127.0.0.1:{listen_port}/mcp")
            for token, group in tokens.items():
                print(f"{group}  Authorization: Bearer {token}")
            uvicorn.run(app, host="127.0.0.1", port=listen_port)
            return

        with http_endpoint(app) as url, httpx.Client(base_url=url, trust_env=False) as http:
            assert (
                http.post(
                    "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                ).status_code
                == 401
            )
            owner, consumer = [AgentClient(http, token, group) for token, group in tokens.items()]
            owner.connect()
            consumer.connect()
            report = reuse_assets(catalog, owner, consumer, refs, root)
        # Restart the HTTP service and open a new Catalog against the same state.
        catalog = Catalog(database)
        with http_endpoint(create_app(catalog, tokens)) as url:
            with httpx.Client(base_url=url, trust_env=False) as http:
                owner, consumer = [
                    AgentClient(http, token, group) for token, group in tokens.items()
                ]
                assert consumer.call("asset_inspect", revision=report)["inputs"]
                verify_sharing(catalog, owner, consumer, refs, report)
        assert (root / "analysis-demo" / "CONTCAR").is_file()
        print("验收通过。MCP 服务重启后版本与血缘仍可读，已下载副本仍存在。")


if __name__ == "__main__":
    main()
