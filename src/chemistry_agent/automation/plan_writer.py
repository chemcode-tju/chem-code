"""Offline authoring helpers copied into the plan program's isolated working directory."""

import inspect
import json
import textwrap
from pathlib import Path


def node_code(function, arguments: dict | None = None) -> str:
    """Serialize a self-contained function; import its dependencies inside the function."""
    source = textwrap.dedent(inspect.getsource(function))
    return (
        f"{source}\n\nif __name__ == '__main__':\n    {function.__name__}(**{arguments or {}!r})\n"
    )


class PlanWriter:
    def __init__(self, title: str, goal: str) -> None:
        self.title = title
        self.goal = goal
        self.nodes: list[dict] = []

    @staticmethod
    def ref(node: str, output: str = "structure") -> dict:
        return {"node": node, "output": output}

    def model(self, key: str, function, *, inputs=None, arguments=None, title=None) -> str:
        self.nodes.append(
            {
                "id": key,
                "title": title or key,
                "kind": "model",
                "code": node_code(function, arguments),
                "inputs": inputs or {},
            }
        )
        return key

    def analysis(
        self,
        key: str,
        function,
        *,
        inputs,
        arguments=None,
        result_files=None,
        file_source="retrieved",
        title=None,
    ) -> str:
        self.nodes.append(
            {
                "id": key,
                "title": title or key,
                "kind": "analysis",
                "code": node_code(function, arguments),
                "inputs": inputs,
                "result_files": result_files or [],
                "file_source": file_source,
            }
        )
        return key

    def vasp(
        self,
        key: str,
        structure: str,
        protocol_id: str,
        *,
        incar: dict,
        resources: dict,
        kmesh,
        require_ionic=False,
        title=None,
    ) -> str:
        self.nodes.append(
            {
                "id": key,
                "title": title or key,
                "kind": "vasp",
                "inputs": {"structure": self.ref(structure)},
                "calculation": {
                    "protocol_id": protocol_id,
                    "incar": incar,
                    "kmesh": kmesh,
                    "resources": resources,
                    "require_ionic": require_ionic,
                },
            }
        )
        return key

    def write(self) -> None:
        Path("result.json").write_text(
            json.dumps(
                {"title": self.title, "goal": self.goal, "nodes": self.nodes}, ensure_ascii=False
            )
        )
