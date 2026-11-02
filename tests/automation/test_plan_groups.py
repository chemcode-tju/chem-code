"""Grouping projects existing provenance without changing the executable graph."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemistry_agent.automation.plan_groups import display_nodes
from chemistry_agent.automation.plan_models import PlanDraft, PlanNode
from chemistry_agent.automation.plan_writer import PlanWriter


def _operation(key: str, parents: tuple[str, ...] = (), **fields: object) -> dict:
    return {
        "id": key,
        "title": key,
        "kind": "model",
        "depends_on": list(parents),
        "inputs": {p: {"node": p} for p in parents},
        **fields,
    }


def test_legacy_candidate_paths_follow_inputs_checkpoints_and_common_ancestry() -> None:
    top = "CFG = {'species':'N2', 'site':'top'}"
    edge = (
        "def make(**kwargs): pass\nif __name__ == '__main__':\n"
        " make(**{'adsorbate':'N2', 'site':'edge'})"
    )
    nodes = [
        _operation("z", ("v",)),  # A plot written after the step in a previous revision.
        _operation("clean"),
        _operation("a", ("clean",), code=top),
        _operation("b", ("clean",), code=edge),
        _operation("c", ("clean",), code=top.replace("N2", "OH")),
        _operation("r", ("a",), kind="vasp", inputs={"structure": {"structure_id": "saved"}}),
        _operation("v", ("r", "b"), kind="analysis"),
        _operation("mix", ("v", "c"), kind="analysis"),
        _operation("repeat", kind="vasp"),
        _operation("unset", ("a",), group_path=[]),
    ]
    plan = {
        "nodes": nodes,
        "runtime": {
            "r": {"attempts": [{"node": {"inputs": {"structure": {"node": "a"}}}}]},
            "repeat": {"retry_of": "r"},
        },
    }
    original = copy.deepcopy(plan)
    paths = {n["id"]: n.get("group_path", []) for n in display_nodes(plan)}
    assert paths == {
        "z": ["N2"],
        "clean": [],
        "a": ["N2", "top"],
        "b": ["N2", "edge"],
        "c": ["OH", "top"],
        "r": ["N2", "top"],
        "v": ["N2"],
        "mix": [],
        "repeat": ["N2", "top"],
        "unset": [],
    }
    assert plan == original


@pytest.mark.parametrize(
    "code",
    [
        "CFG={'species':'X', 'site':unknown()}",
        "CFG={'species':'X', 'site':'edge'}\nCFG['site'] = computed()",
        "CFG={'species':'X'}",
        "CFG={'species':'X', 'site':'edge'}\nif flag: CFG = other",
        "CFG={'species':'X', 'adsorbate':'Y', 'site':'top'}",
        "not valid Python here",
    ],
)
def test_unproven_legacy_labels_leave_an_ordinary_dag(code: str) -> None:
    node = _operation("m_cooh_bridge_0_0", code=code)
    assert "group_path" not in display_nodes({"nodes": [node]})[0]


def _make() -> None:
    pass


def test_writer_roundtrips_generic_labels_and_all_three_node_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    writer = PlanWriter("Screen", "Compare methods")
    writer.model("u", _make, group_path=["Solvent A", "298 K"])
    writer.vasp(
        "v",
        "u",
        "pbe",
        incar={},
        resources={"queue": "batch", "nodes": 1, "wallclock_seconds": 3600},
        kmesh=[1, 1, 1],
        group_path=["Solvent A", "350 K"],
    )
    writer.analysis("w", _make, inputs={"sample": writer.ref("v", "result")}, group_path=[])
    writer.write()
    draft = PlanDraft.model_validate(json.loads(Path("result.json").read_text()))
    assert [n.group_path for n in draft.nodes] == [
        ["Solvent A", "298 K"],
        ["Solvent A", "350 K"],
        [],
    ]
    with pytest.raises(ValidationError):
        PlanNode.model_validate(_operation("q", code="pass", group_path=["  "]))


def test_legacy_labels_are_scoped_to_distinct_structure_inputs() -> None:
    code = "CFG={'species':'O', 'site':'hollow'}"
    nodes = [
        _operation("surface_a"),
        _operation("surface_b"),
        _operation("q", ("surface_a",), code=code),
        _operation("w", ("surface_b",), code=code),
    ]
    rows = display_nodes({"nodes": nodes})
    assert rows[2]["group_path"] == ["surface_a", "O", "hollow"]
    assert rows[3]["group_path"] == ["surface_b", "O", "hollow"]
