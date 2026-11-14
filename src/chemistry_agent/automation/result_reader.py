"""Read-only, dependency-free result adapters (also sent over SSH).

The normalized manifest is software independent. The JSON-path collection adapter
maps an existing producer's fields without moving files or executing its code.
"""

import json
import math
import re
from pathlib import Path


def result_file(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or any(p in {"..", "archive"} for p in relative.parts):
        raise ValueError("结果文件须位于登记作业目录内。")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("结果文件链接越过作业目录。")
    return path


def result_json(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("结果索引超过 8 MiB，请拆分为快照。")
    return json.loads(path.read_text())


def finite_result(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalized_result(root: Path, data: dict) -> dict:
    """Validate portable display data; no code from a producer is evaluated."""
    structures = []
    for item in data.get("structures", []):
        path = result_file(root, item["path"])
        if path.is_file():
            structures.append({"path": item["path"], "label": str(item.get("label", path.name))})
    series = []
    for item in data.get("series", []):
        points = item.get("points", [])
        if any(len(p) != 2 or not all(finite_result(v) for v in p) for p in points):
            raise ValueError("曲线坐标须为有限数值。")
        series.append(
            {key: item.get(key, "") for key in ("label", "x_label", "y_label", "unit")}
            | {"points": points}
        )
    return {"kind": "generic", "structures": structures, "series": series, "snapshots": []}


def endpoint_result(path: Path) -> dict:
    """Read the last complete ionic force/energy pair from a bounded OUTCAR tail.

    Atomic forces are unprojected and include constrained atoms. They must never
    be substituted for NEB forces or silently change the path energy reference.
    """
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 8 * 1024 * 1024))
        text = stream.read().decode("utf-8", errors="replace")
    result, rows, forces = None, None, None
    for line in text.splitlines(keepends=True):
        if not line.endswith(("\n", "\r")):
            break
        if re.search(r"\bIteration\s+\d+\s*\(", line):
            rows, forces = None, None
        if "TOTAL-FORCE (eV/Angst)" in line:
            rows, forces = [], None
            continue
        if rows is not None:
            if re.fullmatch(r"\s*-+\s*", line):
                if rows:
                    forces, rows = rows, None
                continue
            try:
                values = [float(v.replace("D", "E")) for v in line.split()]
                if len(values) != 6 or not all(finite_result(v) for v in values):
                    raise ValueError("Incomplete force row")
                norm = math.hypot(*values[3:])
                if not finite_result(norm):
                    raise ValueError("Invalid force norm")
                rows.append(norm)
            except ValueError:
                rows = None
        match = re.search(r"free\s+energy\s+TOTEN\s*=\s*([-+\d.eEdD]+)", line)
        if match and forces:
            energy = float(match[1].replace("D", "E"))
            if finite_result(energy):
                result = {"energy": energy, "atomic_fmax": max(forces), "atoms": len(forces)}
            forces = None
    if result is None:
        raise ValueError("OUTCAR 尚无完整的离子步能量和原子力记录")
    return result


def path_results(root: Path, source: dict) -> dict:
    """Read only declared path snapshots, never scan force-call directories."""
    pattern = source["pattern"]
    result_file(root, pattern)
    if "**" in pattern:
        raise ValueError("请使用明确的快照目录模式，不递归扫描计算目录。")
    fields = source["fields"]
    snapshots, skipped = [], []
    endpoints = {}
    for end, name in source.get("endpoint_outcars", {}).items():
        try:
            endpoints[end] = endpoint_result(result_file(root, name)) | {"source": name}
        except (OSError, ValueError) as exc:
            skipped.append({"path": name, "reason": str(exc)[:160]})
    for path in root.glob(pattern):
        try:
            relative = str(path.relative_to(root))
            result_file(root, relative)
            raw = result_json(path)
            step, energies = raw[fields["step"]], raw[fields["energy"]]
            force = raw.get(fields["force"])
            if not isinstance(step, int) or isinstance(step, bool) or step < 0:
                raise ValueError("无效的优化步编号")
            if len(energies) < 2 or not all(finite_result(v) for v in energies):
                raise ValueError("路径能量尚不完整")
            if fields.get("valid") and raw.get(fields["valid"]) is not True:
                raise ValueError("生产者尚未确认完整路径")
            if force is not None and (not finite_result(force) or force < 0):
                raise ValueError("无效的路径投影力")
            image_forces = raw.get(fields.get("image_force", "image_projected_fmax_eV_A"))
            # ASE records projected NEB forces only for moving (interior) images.
            # Fixed endpoints have no NEB force; never manufacture zero values.
            if image_forces is not None:
                if len(image_forces) == len(energies) - 2:
                    image_forces = [None, *image_forces, None]
                if len(image_forces) != len(energies) or any(
                    value is not None and (not finite_result(value) or value < 0)
                    for value in image_forces
                ):
                    raise ValueError("每像投影力与路径像数量不匹配或数值无效")
            images = []
            for index, energy in enumerate(energies):
                name = source["image_pattern"].format(index=index)
                image = str(path.parent.relative_to(root) / name)
                target = result_file(root, image)
                if not target.is_file() or not target.stat().st_size:
                    raise ValueError("路径结构尚未写完")
                images.append(
                    {
                        "path": image,
                        "label": f"像 {index}",
                        "index": index,
                        "energy": energy,
                        "relative_energy": energy - energies[0],
                        "force": image_forces[index] if image_forces is not None else None,
                        "endpoint_reference": endpoints.get(
                            "initial"
                            if index == 0
                            else "final"
                            if index == len(energies) - 1
                            else ""
                        ),
                    }
                )
            snapshots.append(
                {
                    "step": step,
                    "images": images,
                    "force": force,
                    "peak_energy": max(energies) - energies[0],
                    "source": relative,
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            skipped.append({"path": str(path.relative_to(root)), "reason": str(exc)[:160]})
    snapshots.sort(key=lambda item: item["step"])
    steps = [item["step"] for item in snapshots]
    if len(steps) != len(set(steps)):
        raise ValueError("同一次执行存在重复路径步编号，请分开登记执行尝试。")
    return {
        "kind": "neb",
        "snapshots": snapshots,
        "structures": [],
        "series": [],
        "energy_label": source.get("energy_label", "能量"),
        "force_label": source.get("force_label", "NEB 投影 Fmax"),
        "force_target": source.get("force_target"),
        "skipped": skipped,
    }


def result_snapshot(
    root: Path, source: dict, requested: str | None = None, path_step: int | None = None
) -> dict:
    if "archive" in root.parts:
        raise ValueError("不能读取 archive 目录。")
    if source["adapter"] == "auto":
        manifest = result_file(root, "display.json")
        if not manifest.is_file():
            # Bounded compatibility discovery for the existing ASE producer;
            # no traversal of the expensive force_calls tree or solver inputs.
            paths = result_file(root, "paths")
            if paths.is_dir() and next(paths.glob("step_*/path.json"), None) is not None:
                return result_snapshot(
                    root,
                    source | {"adapter": "json_paths", "pattern": "paths/step_*/path.json"},
                    requested,
                    path_step,
                )
            if requested is not None:
                raise FileNotFoundError("结果索引尚未生成或当前不可用。")
            return {
                "kind": "generic",
                "available": False,
                "structures": [],
                "series": [],
                "snapshots": [],
            }
        data = normalized_result(root, result_json(manifest))
    elif source["adapter"] == "json_paths":
        data = path_results(root, source)
    elif source["adapter"] == "manifest":
        data = normalized_result(root, result_json(result_file(root, source["path"])))
    else:
        data = normalized_result(root, source)
    if path_step is not None:
        if requested is not None:
            raise ValueError("请选择单个结构或整条路径")
        row = next((r for r in data["snapshots"] if r["step"] == path_step), None)
        if row is None:
            raise ValueError("该步尚无完整路径快照")
        paths = [result_file(root, item["path"]) for item in row["images"]]
        if sum(path.stat().st_size for path in paths) > 32 * 1024 * 1024:
            raise ValueError("路径预览超过 32 MiB，请逐像读取")
        return {
            "kind": "neb_sequence",
            "step": path_step,
            "files": [
                {"name": path.name, "content": path.read_text(), "index": item["index"]}
                for path, item in zip(paths, row["images"], strict=True)
            ],
        }
    if requested is None:
        return data
    allowed = {item["path"] for item in data["structures"]}
    allowed.update(image["path"] for row in data["snapshots"] for image in row["images"])
    if requested not in allowed:
        raise ValueError("此文件未登记为可查看的结构。")
    path = result_file(root, requested)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("结构超过在线快照大小，请使用文件工具读取。")
    return {"name": path.name, "content": path.read_text(), "path": requested}
