"""Synthetic structure, energy and analysis-script versions for the demo."""

import json

from catalog import Asset, Calculation, Catalog, Energy, Method, Script, Source

PRODUCER = "lab-surface"
CONSUMER = "lab-analysis"
METHOD_KEY = "demo-vasp-pbe-paw-450ev-3x3x1-slab-k1"

STRUCTURE = b"""Synthetic CuCO fixture
1.0
5.12 0.00 0.00
0.00 5.12 0.00
0.00 0.00 18.00
Cu C O
1 1 1
Direct
0.50 0.50 0.31
0.50 0.50 0.41
0.50 0.50 0.47
"""

RECIPE_V1 = b"""import json
from pathlib import Path


def main() -> None:
    energy = json.loads(Path("energy.json").read_text(encoding="utf-8"))
    atoms = sum(map(int, Path("CONTCAR").read_text(encoding="utf-8").splitlines()[6].split()))
    output = {"value": round(energy["value"] / atoms, 8), "metric": "TOTEN/N", "unit": "eV/atom"}
    Path("summary.json").write_text(json.dumps(output) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
"""
RECIPE_V2 = RECIPE_V1.replace(b", 8)", b", 4)")


def load_fixture(catalog: Catalog) -> dict[str, str]:
    method = Method(
        engine="VASP",
        engine_version="6.4.3",
        functional="PBE",
        comparison_key=METHOD_KEY,
    )
    calculation = Calculation(
        formula="CCuO",
        method=method,
        energy=Energy(metric="TOTEN", value=-16.12345678),
        scheduler_state="completed",
        electronic_converged=True,
        ionic_converged=True,
    )
    structure = Asset(
        slug="cuco-optimized",
        kind="calculation",
        title="Cu–CO 优化结构",
        description="Synthetic Cu CO slab optimization with a matching CONTCAR and TOTEN.",
        keywords=("Cu", "CO", "Cu100", "slab", "VASP", "PBE", "优化结构", "吸附"),
        source=Source(node="cpu-demo", record_id="sim-job-1001", record_type="job"),
        calculation=calculation,
    )

    def register_calculation(item: Asset) -> str:
        return catalog.register(
            PRODUCER,
            item,
            {
                "CONTCAR": STRUCTURE,
                "energy.json": item.calculation.energy.model_dump_json().encode(),
            },
        )

    good = register_calculation(structure)
    catalog.approve(PRODUCER, good, reviewer="surface-curator")
    catalog.share(PRODUCER, good, CONSUMER)

    r2scan = method.model_copy(update={"functional": "r2SCAN", "comparison_key": "demo-r2scan"})
    incompatible = register_calculation(
        structure.model_copy(
            update={
                "calculation": calculation.model_copy(update={"method": r2scan}),
                "source": structure.source.model_copy(update={"record_id": "sim-job-1002"}),
            }
        )
    )
    catalog.approve(PRODUCER, incompatible, reviewer="surface-curator")
    catalog.share(PRODUCER, incompatible, CONSUMER)

    incomplete = register_calculation(
        structure.model_copy(
            update={
                "calculation": calculation.model_copy(update={"ionic_converged": False}),
                "source": structure.source.model_copy(update={"record_id": "sim-job-1003"}),
            }
        )
    )

    script = Asset(
        slug="energy-summary",
        kind="script",
        title="结构总能按原子数归一",
        description="Normalize VASP TOTEN by atom count for a diagnostic report.",
        keywords=("VASP", "TOTEN", "normalize", "归一化", "后处理"),
        source=Source(node="author-demo", record_id="script-revision-1", record_type="workspace"),
        script=Script(
            entry_point="summarize.py",
            runtime="CPython 3.11+",
            dependencies=(),
            invocation=("python3", "summarize.py"),
            input_contract="CONTCAR and energy.json with the TOTEN metric in eV",
        ),
    )
    old_script = catalog.register(PRODUCER, script, {"summarize.py": RECIPE_V1})
    catalog.approve(PRODUCER, old_script, reviewer="surface-curator")
    catalog.share(PRODUCER, old_script, CONSUMER)
    new_script = catalog.register(
        PRODUCER,
        script.model_copy(
            update={
                "source": script.source.model_copy(update={"record_id": "script-revision-2"}),
            }
        ),
        {"summarize.py": RECIPE_V2},
    )
    catalog.approve(PRODUCER, new_script, reviewer="surface-curator")

    # Replayed publication resolves to the same manifest revision.
    assert good == register_calculation(structure)
    return {
        "calculation": good,
        "incompatible": incompatible,
        "draft": incomplete,
        "script_v1": old_script,
        "script_v2": new_script,
    }


def register_summary(catalog: Catalog, inputs: tuple[str, ...], data: bytes) -> str:
    result = json.loads(data)
    asset = Asset(
        slug="energy-report",
        kind="analysis",
        title="Cu–CO 归一化能量报告",
        description=f"Synthetic post-processing result {result['value']} {result['unit']}.",
        keywords=("VASP", "TOTEN", "normalize"),
        source=Source(node="analysis-demo", record_id="post-2001", record_type="analysis_run"),
        inputs=inputs,
    )
    revision = catalog.register(CONSUMER, asset, {"summary.json": data})
    catalog.approve(CONSUMER, revision, reviewer="analysis-curator")
    return revision
