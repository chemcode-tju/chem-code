"""Validate the downloaded, ignored structure corpus and generate an ASE trajectory."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterator
from itertools import zip_longest
from pathlib import Path
from typing import Any

import numpy as np
from ase import Atoms
from ase import __version__ as ase_version
from ase.data import atomic_numbers
from ase.io import iread
from ase.io.trajectory import Trajectory

from chemistry_agent.structures import convert_structure, detect_format

# Configuration
corpus_directory = "data/cluster-b-structures"
trajectory_source_name = "trajectory.extxyz"


def reference_frames(path: Path, entry: dict[str, Any]) -> Iterator[Atoms]:
    """Read original files with ASE, using recorded LAMMPS type mappings."""
    file_format = entry["format"]
    options: dict[str, Any] = {}
    elements = entry.get("type_map", "").split()
    if file_format == "lammps-data":
        options["Z_of_type"] = {
            index + 1: atomic_numbers[value] for index, value in enumerate(elements)
        }
    if file_format == "lammps-dump-text":
        options["specorder"] = elements
    if file_format.startswith("lammps"):
        options["units"] = entry.get("units", "metal")
    yield from iread(path, format=file_format, index=":", **options)


def describe(atoms: Atoms) -> dict[str, Any]:
    return {
        "symbols": atoms.get_chemical_symbols(),
        "positions": atoms.positions.tolist(),
        "cell": np.asarray(atoms.cell).tolist(),
        "pbc": atoms.pbc.tolist(),
        "masses": atoms.get_masses().tolist(),
    }


def generate_trajectory(source: Path, destination: Path, entry: dict[str, Any]) -> int:
    """Write all source frames and check the generated binary trajectory against them."""
    with Trajectory(destination, "w") as trajectory:
        for atoms in reference_frames(source, entry):
            trajectory.write(atoms)
    count = 0
    generated_entry = {"format": "traj"}
    for original, generated in zip_longest(
        reference_frames(source, entry), reference_frames(destination, generated_entry)
    ):
        if original is None or generated is None:
            raise ValueError("Generated ASE trajectory frame count differs from source.")
        np.testing.assert_array_equal(original.numbers, generated.numbers)
        np.testing.assert_allclose(original.positions, generated.positions, atol=1e-12)
        np.testing.assert_allclose(original.cell, generated.cell, atol=1e-12)
        np.testing.assert_array_equal(original.pbc, generated.pbc)
        count += 1
    return count


def prepare_case(
    path: Path, entry: dict[str, Any], output_directory: Path, index: int
) -> dict[str, Any]:
    """Prepare independent ASE reference coordinates for the browser parser comparison."""
    samples: dict[int, dict[str, Any]] = {}
    count = 0
    for count, atoms in enumerate(reference_frames(path, entry), start=1):
        if count == 1:
            samples[0] = describe(atoms)
        last = describe(atoms)
    if not count:
        raise ValueError(f"No structures found in {path.name}.")
    samples[count - 1] = last
    if count > 2:
        for frame_index, atoms in enumerate(reference_frames(path, entry)):
            if frame_index == count // 2:
                samples[frame_index] = describe(atoms)
                break
    file_format = entry["format"]
    viewer_path = path
    viewer_name = path.name
    if file_format in {"gromacs", "lammps-data", "lammps-dump-text"}:
        converted, converted_count, _ = convert_structure(
            path.read_bytes(),
            path.name,
            file_format,
            entry.get("type_map", ""),
            entry.get("units", "metal"),
        )
        if count != converted_count:
            raise ValueError(f"Converted frame count differs for {path.name}.")
        viewer_path = output_directory / f"converted-{index}.extxyz"
        viewer_path.write_text(converted)
        viewer_name = "converted.extxyz"
    return {
        "source": str(path),
        "format": file_format,
        "path": str(viewer_path),
        "viewer_name": viewer_name,
        "count": count,
        "samples": samples,
        "atoms_first": len(samples[0]["symbols"]),
    }


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    corpus = repository / corpus_directory
    manifest_path = corpus / "selected.json"
    if not manifest_path.is_file():
        raise SystemExit(f"Missing local corpus manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    output_directory = corpus / "validation"
    output_directory.mkdir(exist_ok=True)
    (output_directory / "report.json").write_text(json.dumps({"status": "running"}))
    cases = []
    trajectory_source: tuple[Path, dict[str, Any]] | None = None
    for index, entry in enumerate(manifest["files"]):
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Corpus manifest must contain relative paths.")
        if any("archive" in part.lower() for part in relative.parts):
            raise ValueError("Archive folders are excluded from validation.")
        path = corpus / "files" / relative
        if not path.is_file():
            raise FileNotFoundError(f"Missing downloaded structure: {path}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"Downloaded structure checksum changed: {path}")
        with path.open() as stream:
            prefix = stream.read(1024)
        entry["format"] = detect_format(path.name, prefix)
        case = prepare_case(path, entry, output_directory, index)
        cases.append(case)
        print(
            f"ASE read: {relative} — {case['count']} frames, {case['atoms_first']} atoms",
            flush=True,
        )
        if path.name == trajectory_source_name and trajectory_source is None:
            trajectory_source = path, entry
    if trajectory_source is None:
        raise ValueError(f"No source named {trajectory_source_name} in the corpus.")
    generated_directory = corpus / "generated"
    generated_directory.mkdir(exist_ok=True)
    generated_path = generated_directory / "ase.traj"
    count = generate_trajectory(trajectory_source[0], generated_path, trajectory_source[1])
    with generated_path.open("rb") as stream:
        generated_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    (generated_directory / "provenance.json").write_text(
        json.dumps(
            {
                "source": str(trajectory_source[0]),
                "source_sha256": trajectory_source[1]["sha256"],
                "generated": generated_path.name,
                "sha256": generated_hash,
                "frames": count,
                "ase_version": ase_version,
                "all_frames_compared": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    cases.append(prepare_case(generated_path, {"format": "traj"}, output_directory, len(cases)))
    print(f"ASE trajectory generated and compared: {count} frames", flush=True)
    reference_path = output_directory / "reference.json"
    reference_path.write_text(json.dumps({"cases": cases}, ensure_ascii=False))
    subprocess.run(["node", str(repository / "scripts/validate_structure_files.cjs")], check=True)


if __name__ == "__main__":
    main()
