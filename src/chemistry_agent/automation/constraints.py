"""Lossless ASE constraint transport and VASP fractional-coordinate mobility flags."""

import io
import json

import numpy as np
from ase import Atoms
from ase.constraints import FixAtoms, FixCartesian, FixScaled, dict2constraint
from ase.io import write

from chemistry_agent.errors import AgentError

CONSTRAINT_KEY = "chem_agent_constraints"


def positions_dof(atoms: Atoms) -> list[list[bool]]:
    """Combine restrictions; True means movable, opposite to ASE constraint masks."""
    movable = np.ones((len(atoms), 3), dtype=bool)
    for constraint in atoms.constraints:
        indices = constraint.get_indices()
        if isinstance(constraint, FixAtoms):
            movable[indices] = False
        elif isinstance(constraint, (FixScaled, FixCartesian)):
            mask = np.asarray(constraint.mask, dtype=bool)
            if isinstance(constraint, FixCartesian):
                # A Cartesian constraint is representable only if every lattice vector
                # lies entirely in its fixed or free Cartesian subspace.
                fixed = np.linalg.norm(atoms.cell.array[:, mask], axis=1) > 1e-10
                free = np.linalg.norm(atoms.cell.array[:, ~mask], axis=1) > 1e-10
                if np.any(fixed & free):
                    raise AgentError(
                        "unsupported_constraint",
                        "此倾斜晶胞的 Cartesian 方向约束不能等价转换为 VASP；"
                        "请使用 FixScaled 或固定整个原子。",
                        422,
                    )
                mask = fixed
            movable[np.ix_(indices, np.flatnonzero(mask))] = False
        else:
            raise AgentError(
                "unsupported_constraint",
                f"VASP Selective Dynamics 不能表示 {type(constraint).__name__}；"
                "支持 FixAtoms、FixScaled 以及可等价转换的 FixCartesian。",
                422,
            )
    return movable.tolist()


def restore_constraints(atoms: Atoms) -> None:
    """Restore constraints that standard extxyz cannot represent (e.g. FixScaled)."""
    encoded = atoms.info.pop(CONSTRAINT_KEY, None)
    if encoded is not None:
        records = json.loads(encoded) if isinstance(encoded, str) else encoded
        if not isinstance(records, list) or len(records) > 10000:
            raise ValueError("原子约束元数据格式错误。")
        if any(
            not isinstance(item, dict)
            or item.get("name") not in {"FixAtoms", "FixScaled", "FixCartesian"}
            for item in records
        ):
            raise ValueError("不支持的约束元数据。")
        atoms.set_constraint([dict2constraint(item) for item in records])


def structure_text(atoms: Atoms) -> str:
    """Preserve all supported constraints without depending on extxyz move_mask alone."""
    positions_dof(atoms)
    copy = atoms.copy()
    # NumPy arrays in ASE todict need conversion before JSON serialization.
    copy.info.pop(CONSTRAINT_KEY, None)
    if any(isinstance(constraint, FixScaled) for constraint in atoms.constraints):
        copy.info[CONSTRAINT_KEY] = json.dumps(
            [constraint.todict() for constraint in atoms.constraints],
            default=lambda value: value.tolist(),
        )
    buffer = io.StringIO()
    write(buffer, copy, format="extxyz")
    return buffer.getvalue()


def poscar_text(atoms: Atoms) -> str:
    """Normalize directional constraints to VASP's fractional flags before ASE writes."""
    copy = atoms.copy()
    flags = positions_dof(atoms)
    copy.set_constraint(
        [
            FixScaled(index, mask=~np.asarray(row, dtype=bool))
            for index, row in enumerate(flags)
            if not all(row)
        ]
    )
    buffer = io.StringIO()
    write(buffer, copy, format="vasp", direct=True)
    return buffer.getvalue()
