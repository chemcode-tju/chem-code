"""Read-only batch inspection, invoked once by the 30-minute heartbeat."""

# ruff: noqa: E402
# Configuration
base_url = "http://127.0.0.1:8766"
record_file = "data/overnight-2026-11-02/cases.json"

import json
from collections import Counter
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def get(path: str) -> dict:
    with build_opener(ProxyHandler({})).open(base_url + path, timeout=30) as response:
        return json.load(response)


def main() -> None:
    files = [Path(record_file), Path(record_file).with_name("long-cases.json")]
    for case in (row for path in files if path.exists() for row in json.loads(path.read_text())):
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
            print("  states", dict(Counter(s["status"] for s in plan["runtime"].values())))
            print(
                "  dispatch",
                dict(
                    Counter(
                        s["job"].get("scheduler_state")
                        or s["job"].get("submission_state", "unknown")
                        for s in plan["runtime"].values()
                        if s["status"] == "running" and s.get("job")
                    )
                ),
            )
            print(
                "  VASP",
                sum(n["kind"] == "vasp" for n in plan["nodes"]),
                "AiiDA",
                [s["job"]["pk"] for s in plan["runtime"].values() if s.get("job")][-8:],
            )
            for node in plan["nodes"]:
                state = plan["runtime"][node["id"]]
                if len(plan["nodes"]) > 15 and state["status"] != "failed":
                    continue
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
