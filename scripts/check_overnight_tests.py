"""Read-only batch inspection, invoked once by the 30-minute heartbeat."""

# ruff: noqa: E402
# Configuration
base_url = "http://127.0.0.1:8766"
record_file = "data/overnight-2026-11-02/cases.json"

import json
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def get(path: str) -> dict:
    with build_opener(ProxyHandler({})).open(base_url + path, timeout=30) as response:
        return json.load(response)


def main() -> None:
    for case in json.loads(Path(record_file).read_text()):
        sid = case["session_id"]
        plans = get(f"/api/automation/chat/{sid}/workflows")["plans"]
        queue = get(f"/sessions/{sid}/chat-requests")
        session = get(f"/sessions/{sid}")
        Path(f"/private/tmp/chem-night-{sid}.json").write_text(
            json.dumps(
                {"plans": plans, "queue": queue, "session": session}, ensure_ascii=False, indent=2
            )
        )
        print(case["title"], [(r["status"], r.get("error")) for r in queue["requests"]])
        for plan in plans:
            print(" PLAN", plan["id"], "v", plan["revision"], plan["status"], len(plan["nodes"]))
            for node in plan["nodes"]:
                state = plan["runtime"][node["id"]]
                calc = node.get("effective", {})
                print(
                    " ",
                    node["id"],
                    node["kind"],
                    state["status"],
                    state.get("job", {}).get("pk"),
                    state.get("error"),
                    calc.get("incar", {}).get("ENCUT"),
                    calc.get("kmesh"),
                    calc.get("resources", {}).get("queue"),
                )
        messages = [
            m["content"]
            for m in session["messages"]
            if m.get("role") == "assistant" and m.get("content")
        ]
        if messages:
            print(" REPLY", messages[-1][:1000])


if __name__ == "__main__":
    main()
