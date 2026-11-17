"""Exercise both real stdio plugin processes against the separately running daemon."""
# ruff: noqa: E402

# Configuration
workspace_id = "11111111-1111-4111-8111-111111111111"
demo_thread_id = "22222222-2222-4222-8222-222222222222"

import json
import subprocess
from pathlib import Path


class MCPClient:
    def __init__(self, package: Path) -> None:
        self.process = subprocess.Popen(
            ["python3", str(package / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.sequence = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "chem-demo", "version": "1"},
            },
        )

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
            + "\n"
        )
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline())
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    def call(self, name: str, arguments: dict) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(result["content"])
        return result["structuredContent"]

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    monitor = MCPClient(root / "plugins/chem-job-monitor")
    research = MCPClient(root / "plugins/chem-research")
    scope = {"workspace_id": workspace_id}
    project = root / "data/research-plugins/demo-project"
    for folder in ("01-prepare", "02-co-top", "03-co-bridge", "04-compare"):
        (project / folder).mkdir(parents=True, exist_ok=True)
        (project / folder / "README.md").write_text(
            "# 插件演示\n仅用于文件导航测试，无真实计算。\n"
        )
    (project / "02-co-top/outputs").mkdir(exist_ok=True)
    (project / "02-co-top/outputs/summary.txt").write_text("模拟作业记录；非真实科研结果。\n")
    for directory in (".config", "archive"):
        (project / "02-co-top" / directory).mkdir(exist_ok=True)
        (project / "02-co-top" / directory / "README.md").write_text("文件导航演示。\n")
    (project / "02-co-top/.metadata").write_text("仅用于隐藏文件导航测试。\n")
    directory_link = project / "02-co-top/outputs-link"
    if not directory_link.is_symlink():
        directory_link.symlink_to("outputs", target_is_directory=True)
    try:
        for client in (monitor, research):
            client.call(
                "workspace_open", scope | {"title": "Cu–CO 模拟研究", "project_path": str(project)}
            )
        plans = research.call("research_read_plan", scope)["plans"]
        plan = (
            plans[0]
            if plans
            else research.call(
                "research_save_plan",
                scope
                | {
                    "draft": {
                        "title": "Cu 表面 CO 吸附 · 插件演示",
                        "goal": "验证依赖、模拟作业和结果显示",
                        "nodes": [
                            {"id": "prepare", "title": "准备 Cu 表面与 CO 分子", "kind": "task"},
                            {
                                "id": "top",
                                "title": "优化顶位 CO 吸附结构",
                                "kind": "task",
                                "depends_on": ["prepare"],
                                "purpose": "模拟数据，仅验证插件链路。",
                            },
                            {
                                "id": "bridge",
                                "title": "优化桥位 CO 吸附结构",
                                "kind": "task",
                                "depends_on": ["prepare"],
                            },
                            {
                                "id": "compare",
                                "title": "比较吸附能并核对收敛",
                                "kind": "task",
                                "depends_on": ["top", "bridge"],
                            },
                        ],
                    }
                },
            )
        )
        job = monitor.call(
            "monitor_register_job",
            scope
            | {
                "key": "demo-top-v1",
                "name": "顶位模拟作业",
                "duration_seconds": 2,
                "plan_id": plan["id"],
                "node_id": "top",
            },
        )
        wait = monitor.call(
            "monitor_register_wait", scope | {"thread_id": demo_thread_id, "job_ids": [job["id"]]}
        )
        # Closing the subscribing process must not end the durable wait or observer.
        monitor.close()
        monitor = MCPClient(root / "plugins/chem-job-monitor")
        ready = monitor.call(
            "monitor_read_wait", scope | {"wait_id": wait["id"], "timeout_seconds": 10}
        )
        assert ready["status"] in {"ready", "acknowledged"}, ready
        monitor.call(
            "monitor_control_wait",
            scope
            | {"wait_id": wait["id"], "action": "acknowledge", "event_id": ready["event"]["id"]},
        )
        for node, folder in (
            ("prepare", "01-prepare"),
            ("top", "02-co-top"),
            ("bridge", "03-co-bridge"),
            ("compare", "04-compare"),
        ):
            research.call(
                "research_link_folder",
                scope | {"plan_id": plan["id"], "node_id": node, "path": folder},
            )
        files = research.call(
            "research_read_folder", scope | {"plan_id": plan["id"], "node_id": "top"}
        )
        assert {entry["name"] for entry in files["entries"]} == {
            "README.md",
            "outputs",
            ".config",
            "archive",
            ".metadata",
            "outputs-link",
        }
        shared = research.call("research_read_plan", scope)
        assert shared["jobs"][0]["scheduler_state"] == "COMPLETED"
        assert shared["plans"][0]["runtime"]["top"]["status"] == "pending"
        for client in (monitor, research):
            descriptor = client.request("tools/list", {})["tools"][0]
            assert descriptor["_meta"]["openai/ui"]["entrypoints"] == [{"type": "thread"}]
            resource = client.request(
                "resources/read", {"uri": descriptor["_meta"]["ui"]["resourceUri"]}
            )
            assert "<html" in resource["contents"][0]["text"]
        print(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "plan_id": plan["id"],
                    "wait_id": wait["id"],
                    "event_id": ready["event"]["id"],
                    "stdio_restart": "passed",
                    "shared_backend": "passed",
                    "automatic_wakeup": "not_verified",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        monitor.close()
        research.close()


if __name__ == "__main__":
    main()
