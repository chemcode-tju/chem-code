"""ASE calculation-input validation; no calculation engine or database required."""

import hashlib
import io
from pathlib import Path

import numpy as np
from ase import Atoms
from ase.io import iread

from chemistry_agent.automation.constraints import (
    poscar_text,
    positions_dof,
    restore_constraints,
    structure_text,
)
from chemistry_agent.automation.models import StructureInput
from chemistry_agent.errors import AgentError


def read_structure(specification: StructureInput) -> Atoms:
    return _single_structure(io.StringIO(specification.content), specification.format)


def _single_structure(source: Path | io.StringIO, fmt: str) -> Atoms:
    """Stop at the second frame. A calculation input explicitly represents one geometry."""
    try:
        frames = iread(source, format=fmt, index=":")
        try:
            atoms = next(frames, None)
            if atoms is None or next(frames, None) is not None:
                raise ValueError("计算输入需要选定一帧结构。")
        finally:
            frames.close()
        restore_constraints(atoms)
        if not 1 <= len(atoms) <= 10000:
            raise ValueError("结构原子数必须为 1–10000。")
        if not np.isfinite(atoms.positions).all() or not np.isfinite(atoms.cell.array).all():
            raise ValueError("结构包含非有限坐标。")
        if atoms.cell.rank != 3 or abs(atoms.cell.volume) < 1e-6 or not atoms.pbc.all():
            raise ValueError("首版只接受具有完整晶胞的三维周期结构。")
        positions_dof(atoms)
        if np.any(atoms.get_initial_magnetic_moments()) or np.any(atoms.get_initial_charges()):
            raise ValueError("结构含初始磁矩或电荷，请显式转为 INCAR 配置后再导入。")
        return atoms
    except (ValueError, TypeError, IndexError, KeyError, RuntimeError) as exc:
        raise AgentError("invalid_structure", str(exc), 422) from exc


def validate_structure(payload: dict) -> dict:
    specification = StructureInput.model_validate(payload)
    atoms = read_structure(specification)
    return _validated_record(
        atoms,
        specification.model_dump(),
        hashlib.sha256(specification.content.encode()).hexdigest(),
    )


def validate_structure_file(payload: dict) -> dict:
    """The server resolves the file ID to its original bytes before calling this worker."""
    path = Path(payload["file"]) if "file" in payload else None
    atoms = _single_structure(path or io.StringIO(payload["text"]), payload["format"])
    if path:
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    else:
        digest = hashlib.sha256(payload["text"].encode()).hexdigest()
    return _validated_record(atoms, {"format": "vasp", "content": poscar_text(atoms)}, digest)


def _validated_record(atoms: Atoms, structure: dict, digest: str) -> dict:
    return {
        "structure": structure,
        "extxyz": structure_text(atoms),
        "positions_dof": positions_dof(atoms),
        "formula": atoms.get_chemical_formula(),
        "atoms": len(atoms),
        "elements": sorted(set(atoms.get_chemical_symbols())),
        "cell_angstrom": atoms.cell.tolist(),
        "volume_angstrom3": atoms.get_volume(),
        "input_sha256": digest,
    }
