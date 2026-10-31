"""Convert uploaded structures to viewer extxyz, without persisting user files."""

import asyncio
import re
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Literal

import numpy as np
from ase import Atoms
from ase.data import atomic_numbers
from ase.io import iread, read
from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from chemistry_agent.errors import AgentError

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_ATOMS = 100_000
MAX_TOTAL_ATOMS = 1_000_000
MAX_FRAMES = 10_000
FORMATS = {
    "extxyz",
    "vasp",
    "vasp-xdatcar",
    "lammps-dump-text",
    "lammps-data",
    "gromacs",
    "proteindatabank",
}
router = APIRouter(prefix="/structures", tags=["structures"])


def detect_format(filename: str, text: str) -> str:
    """Use explicit formats only; never let ASE resolve a user-supplied path."""
    name = Path(filename).name.lower()
    suffix = Path(name).suffix
    if text.lstrip().startswith("ITEM: TIMESTEP"):
        return "lammps-dump-text"
    if name.startswith("xdatcar") or suffix == ".xdatcar":
        return "vasp-xdatcar"
    if name.startswith(("poscar", "contcar")) or suffix in {".vasp", ".poscar", ".contcar"}:
        return "vasp"
    formats = {
        ".xyz": "extxyz",
        ".extxyz": "extxyz",
        ".gro": "gromacs",
        ".pdb": "proteindatabank",
        ".data": "lammps-data",
        ".lmp": "lammps-data",
        ".lammpstrj": "lammps-dump-text",
        ".dump": "lammps-dump-text",
    }
    if suffix not in formats:
        raise ValueError("无法识别文件格式，请在导入选项中指定格式；暂不支持 XTC/TRR/TPR。")
    return formats[suffix]


def parse_elements(type_map: str) -> list[str]:
    elements = [value for value in re.split(r"[\s,]+", type_map.strip()) if value]
    if any(value not in atomic_numbers or value == "X" for value in elements):
        raise ValueError("元素映射须使用元素符号，例如 Cu O H，依次对应 type 1、2、3。")
    return elements


def atom_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= MAX_ATOMS:
        raise ValueError(f"每帧原子数必须为 1–{MAX_ATOMS}。")
    return count


def required_line(stream: StringIO) -> str:
    line = stream.readline()
    if not line:
        raise ValueError("文件末尾存在不完整帧。")
    return line


def dump_frames(text: str, elements: list[str], units: str) -> Iterator[Atoms]:
    """Parse one dump frame at a time, retaining IDs and the box origin correctly."""
    stream = StringIO(text)
    while line := stream.readline():
        if not line.strip():
            continue
        if line.strip() != "ITEM: TIMESTEP":
            raise ValueError("LAMMPS dump 应以 ITEM: TIMESTEP 开始。")
        rows = [line, required_line(stream), required_line(stream)]
        if rows[-1].strip() != "ITEM: NUMBER OF ATOMS":
            raise ValueError("LAMMPS dump 缺少 NUMBER OF ATOMS。")
        rows.append(required_line(stream))
        count = atom_count(rows[-1])
        rows.append(required_line(stream))
        if not rows[-1].startswith("ITEM: BOX BOUNDS"):
            raise ValueError("LAMMPS dump 缺少 BOX BOUNDS。")
        rows.extend(required_line(stream) for _ in range(3))
        rows.append(required_line(stream))
        if not rows[-1].startswith("ITEM: ATOMS "):
            raise ValueError("LAMMPS dump 缺少 ATOMS 列定义。")
        columns = rows[-1].split()[2:]
        atom_rows = [required_line(stream).split() for _ in range(count)]
        if any(len(row) != len(columns) for row in atom_rows):
            raise ValueError("LAMMPS dump 原子列数不一致。")
        if "element" not in columns:
            if not elements or "type" not in columns:
                raise ValueError("LAMMPS dump 未提供 element 列，请填写 type 对应的元素。")
            # Add explicit elements so ASE cannot silently interpret type as atomic number,
            # or override the supplied mapping by guessing from masses.
            for row in atom_rows:
                atom_type = int(row[columns.index("type")])
                if not 1 <= atom_type <= len(elements):
                    raise ValueError("元素映射未覆盖全部 LAMMPS type。")
                row.append(elements[atom_type - 1])
            rows[-1] = rows[-1].rstrip() + " element\n"
        block = "".join(rows) + "".join(" ".join(row) + "\n" for row in atom_rows)
        atoms = read(StringIO(block), format="lammps-dump-text", units=units, order=True)
        order = np.arange(count)
        if "id" in columns:
            identifiers = np.array([int(row[columns.index("id")]) for row in atom_rows])
            if len(set(identifiers)) != count or np.any(identifiers < 1):
                raise ValueError("LAMMPS 原子 ID 必须为互不重复的正整数。")
            order = np.argsort(identifiers)
            atoms.set_array("id", identifiers[order])
        if "mass" in columns:
            atoms.set_masses([float(atom_rows[i][columns.index("mass")]) for i in order])
        # ASE scaled positions already start at zero; Cartesian dump coordinates do not.
        if "x" in columns or "xu" in columns:
            atoms.positions -= np.asarray(atoms.get_celldisp()).reshape(3)
        yield atoms


def gro_frames(text: str) -> Iterator[Atoms]:
    """GRO may concatenate frames; ASE's GRO reader accepts one frame at a time."""
    stream = StringIO(text)
    while title := stream.readline():
        count_line = required_line(stream)
        count = atom_count(count_line)
        rows = [required_line(stream) for _ in range(count)]
        box = required_line(stream)
        box_values = np.array([float(value) for value in box.split()])
        if len(box_values) not in {3, 9} or not np.isfinite(box_values).all():
            raise ValueError("GRO 晶胞必须包含 3 或 9 个有限数值。")
        atoms = read(StringIO(title + count_line + "".join(rows) + box), format="gromacs")
        # GRO serials wrap at 100000; matching remains by element/order instead of IDs.
        if not np.any(box_values):
            atoms.pbc = False
        yield atoms


def data_frame(text: str, elements: list[str], units: str, atom_style: str) -> Atoms:
    if not elements:
        raise ValueError("LAMMPS data 需要明确提供 type 对应的元素，例如 Cu O H。")
    atoms = read(
        StringIO(text),
        format="lammps-data",
        units=units,
        Z_of_type={index + 1: atomic_numbers[value] for index, value in enumerate(elements)},
        atom_style=None if atom_style == "auto" else atom_style,
    )
    # Conventional data boxes use absolute coordinates; ASE omits xlo/ylo/zlo in celldisp.
    origin = np.asarray(atoms.get_celldisp()).reshape(3).copy()
    for index, axis in enumerate("xyz"):
        match = re.search(rf"^\s*(\S+)\s+\S+\s+{axis}lo\s+{axis}hi\b", text, re.M)
        if match:
            origin[index] = float(match[1])
    atoms.positions -= origin
    return atoms


def frame_text(atoms: Atoms) -> str:
    """Serialize only viewer fields; coordinates/cell in Å and masses in atomic mass units."""
    atom_count(str(len(atoms)))
    masses = atoms.get_masses()
    if not all(np.isfinite(values).all() for values in (atoms.positions, atoms.cell, masses)):
        raise ValueError("结构包含非有限坐标、晶胞或质量。")
    if np.any(masses <= 0) or np.any(atoms.numbers == 0):
        raise ValueError("无法识别某些原子的元素或质量，请核对原子名/元素映射。")
    if np.any(atoms.pbc) and np.linalg.matrix_rank(atoms.cell) < sum(atoms.pbc):
        raise ValueError("周期晶胞退化，无法进行 Wrap 或晶胞显示。")
    lattice = " ".join(f"{value:.12g}" for value in np.asarray(atoms.cell).ravel())
    periodic = " ".join("T" if value else "F" for value in atoms.pbc)
    identifiers = atoms.arrays.get("id")
    properties = "species:S:1:pos:R:3:mass:R:1" + (":id:I:1" if identifiers is not None else "")
    lines = [str(len(atoms)), f'Lattice="{lattice}" Properties={properties} pbc="{periodic}"']
    for index, (symbol, position, mass) in enumerate(
        zip(atoms.get_chemical_symbols(), atoms.positions, masses, strict=True)
    ):
        coordinates = " ".join(f"{value:.12g}" for value in position)
        identifier = f" {int(identifiers[index])}" if identifiers is not None else ""
        lines.append(f"{symbol} {coordinates} {mass:.12g}{identifier}")
    return "\n".join(lines) + "\n"


def convert_structure(
    content: bytes,
    filename: str,
    file_format: str = "auto",
    type_map: str = "",
    units: str = "metal",
    atom_style: str = "auto",
) -> tuple[str, int, str]:
    """Return extxyz, frame count and resolved format, rejecting partial conversions."""
    try:
        text = content.decode("utf-8-sig")
        selected = detect_format(filename, text) if file_format == "auto" else file_format
        if selected not in FORMATS:
            raise ValueError("不支持此文件格式。")
        elements = parse_elements(type_map)
        if selected == "lammps-dump-text":
            frames = dump_frames(text, elements, units)
        elif selected == "lammps-data":
            frames = iter([data_frame(text, elements, units, atom_style)])
        elif selected == "gromacs":
            frames = gro_frames(text)
        else:
            frames = iread(StringIO(text), format=selected, index=":")
        output = StringIO()
        total_atoms = frame_count = 0
        for frame_count, atoms in enumerate(frames, start=1):
            total_atoms += len(atoms)
            if frame_count > MAX_FRAMES or total_atoms > MAX_TOTAL_ATOMS:
                raise ValueError("结构转换超出帧数或总原子数限制，请先拆分轨迹。")
            output.write(frame_text(atoms))
            if output.tell() > MAX_OUTPUT_BYTES:
                raise ValueError("转换结果超过 64 MiB，请先拆分轨迹。")
        if frame_count == 0:
            raise ValueError("文件中没有有效结构帧。")
        return output.getvalue(), frame_count, selected
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        RuntimeError,
        OSError,
        AssertionError,
    ) as exc:
        raise AgentError("invalid_structure", f"结构读取失败：{exc}", 422) from exc


@router.post("/convert", response_class=PlainTextResponse)
async def convert_upload(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    file_format: str = Query(default="auto", alias="format", max_length=32),
    type_map: str = Query(default="", max_length=1000),
    units: Literal["metal", "real"] = "metal",
    atom_style: Literal["auto", "atomic", "full", "charge", "molecular"] = "auto",
) -> PlainTextResponse:
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_UPLOAD_BYTES:
            raise AgentError("structure_too_large", "转换上传上限为 32 MiB。", 413)
        chunks.extend(chunk)
    converted, count, selected = await asyncio.to_thread(
        convert_structure,
        bytes(chunks),
        filename,
        file_format,
        type_map,
        units,
        atom_style,
    )
    return PlainTextResponse(
        converted, headers={"X-Structure-Frames": str(count), "X-Structure-Format": selected}
    )
