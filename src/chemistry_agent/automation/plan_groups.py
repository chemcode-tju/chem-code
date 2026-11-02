"""Read-only display paths for authored groups and older adsorption candidates."""

import ast
from collections import defaultdict, deque
from functools import lru_cache


def _candidate_labels(mapping: ast.expr) -> tuple[str, ...]:
    if not isinstance(mapping, ast.Dict):
        return ()
    labels: dict[str, str] = {}
    for key, value in zip(mapping.keys, mapping.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return ()  # A dynamic key or **expansion could override a label.
        if key.value not in {"species", "adsorbate", "site", "facet"}:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return ()
        label = value.value.strip()
        if not label or len(label) > 80 or key.value in labels:
            return ()
        labels[key.value] = label
    species = labels.get("adsorbate") or labels.get("species")
    if not species or not labels.get("site") or labels.get("species", species) != species:
        return ()
    if "facet" in labels:
        return labels["facet"], species, labels["site"]
    return species, labels["site"]


@lru_cache(maxsize=256)
def _literal_candidate(code: str) -> tuple[str, ...]:
    """Recognize the two historical generator forms without interpreting model code."""
    try:
        body = ast.parse(code).body
    except (SyntaxError, ValueError, RecursionError):
        return ()
    assignments = {
        id(target)
        for stmt in body
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Name) and target.id == "CFG"
    }
    if len(assignments) > 1:
        return ()
    for statement in body:
        for expr in ast.walk(statement):
            if isinstance(expr, ast.Name) and expr.id == "CFG" and isinstance(expr.ctx, ast.Store):
                if id(expr) not in assignments:
                    return ()
            if isinstance(expr, (ast.Subscript, ast.Attribute)) and isinstance(
                expr.value, ast.Name
            ):
                if expr.value.id == "CFG" and (
                    not isinstance(expr.ctx, ast.Load)
                    or isinstance(expr, ast.Attribute)
                    and expr.attr != "get"
                ):
                    return ()
    functions = {stmt.name for stmt in body if isinstance(stmt, ast.FunctionDef)}
    candidates = set()
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CFG" for target in stmt.targets
        ):
            candidates.add(_candidate_labels(stmt.value))
        match stmt:
            case ast.If(
                test=ast.Compare(
                    left=ast.Name(id="__name__"),
                    ops=[ast.Eq()],
                    comparators=[ast.Constant(value="__main__")],
                )
            ):
                pass
            case _:
                continue
        for call_stmt in stmt.body:
            if not isinstance(call_stmt, ast.Expr) or not isinstance(call_stmt.value, ast.Call):
                continue
            call = call_stmt.value
            if isinstance(call.func, ast.Name) and call.func.id in functions:
                for keyword in call.keywords:
                    if keyword.arg is None:
                        candidates.add(_candidate_labels(keyword.value))
    return candidates.pop() if len(candidates) == 1 else ()


def _sources(node: dict, state: dict) -> list[str | None]:
    """Recover the original structure link when a relaxation restarts from a checkpoint."""
    if state.get("retry_of"):
        return [state["retry_of"]]
    inputs = node.get("inputs", {})
    if node.get("kind") == "vasp":
        source = inputs.get("structure", {}).get("node")
        if source:
            return [source]
        previous = {
            attempt.get("node", {}).get("inputs", {}).get("structure", {}).get("node")
            for attempt in state.get("attempts", [])
        } - {None}
        if len(previous) == 1 and previous.issubset(node.get("depends_on", [])):
            return list(previous)
    if inputs:
        return list(dict.fromkeys(ref.get("node") for ref in inputs.values()))
    return list(dict.fromkeys(node.get("depends_on", [])))


def _common_path(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    shared = []
    for level in zip(*paths, strict=False):
        if len(set(level)) != 1:
            break
        shared.append(level[0])
    return tuple(shared)


def _historical_candidates(nodes: list[dict]) -> dict[str, tuple[str, ...]]:
    """Keep equal adsorption labels on different upstream surfaces distinguishable."""
    candidates = {}
    origins = {}
    for node in nodes:
        code = node.get("code") or ""
        if (
            node.get("group_path") is not None
            or node["kind"] != "model"
            or len(code) > 32_000
            or "site" not in code
        ):
            continue
        path = _literal_candidate(code)
        if not path:
            continue
        key = node["id"]
        candidates[key] = path
        structures = [
            r
            for r in node.get("inputs", {}).values()
            if r.get("output", "structure") == "structure"
        ]
        if len(path) == 2 and len(structures) == 1 and structures[0].get("node"):
            origins[key] = structures[0]["node"]
    if len(set(origins.values())) > 1:
        for key, origin in origins.items():
            candidates[key] = (origin, *candidates[key])
    return candidates


def display_nodes(plan: dict) -> list[dict]:
    """Project paths over real inputs. A merge inherits only its common prefix."""
    nodes = plan["nodes"]
    paths = _historical_candidates(nodes)
    parents: dict[str, list[str]] = {}
    dependents: dict[str, list[str]] = defaultdict(list)
    remaining: dict[str, int] = {}
    for node in nodes:
        key = node["id"]
        if node.get("group_path") is not None:
            paths[key] = tuple(node["group_path"])
            continue
        if key in paths:
            continue
        sources = _sources(node, plan.get("runtime", {}).get(key, {}))
        if not sources or None in sources:
            paths[key] = ()
            continue
        parents[key] = sources
        remaining[key] = len(sources)
        for parent in sources:
            dependents[parent].append(key)
    ready = deque(paths)
    while ready:
        processed = ready.popleft()
        for child in dependents[processed]:
            remaining[child] -= 1
            if not remaining[child]:
                paths[child] = _common_path([paths[key] for key in parents[child]])
                ready.append(child)
    return [
        {**node, "group_path": list(paths[node["id"]])} if paths.get(node["id"]) else node
        for node in nodes
    ]
