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


def repair_literal_newlines(source: str) -> str:
    """Opt-in repair for a generated string split by JSON/newline escaping.

    Only propose a new source if the input has an unterminated literal, the only
    edits are escaping physical newlines within ordinary literals, and it compiles.
    The caller can inspect or reject the result before executing it.
    """
    try:
        compile(source, "previous.py", "exec")
        return source
    except SyntaxError as error:
        if "unterminated string literal" not in error.msg:
            raise
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(source):
        char = source[index]
        if quote:
            if source.startswith(quote, index):
                out.append(quote)
                index += len(quote)
                quote = ""
                continue
            if char == "\\" and index + 1 < len(source):
                out.append(source[index : index + 2])
                index += 2
                continue
            out.append("\\n" if char == "\n" and len(quote) == 1 else char)
        elif char == "#":
            end = source.find("\n", index)
            end = len(source) if end < 0 else end
            out.append(source[index:end])
            index = end
            continue
        elif char in {"'", '"'}:
            quote = char * 3 if source.startswith(char * 3, index) else char
            out.append(quote)
            index += len(quote)
            continue
        else:
            out.append(char)
        index += 1
    fixed = "".join(out)
    compile(fixed, "repaired.py", "exec")
    return fixed
