"""Bounded directory navigation; file content is opened by Codex/Atom Lens."""

from pathlib import Path


def checked_directory(path: Path, roots: tuple[Path, ...]) -> Path:
    if "archive" in path.parts or any(
        part.startswith(".") and part not in {".", ".."} for part in path.parts
    ):
        raise ValueError("不能浏览 archive 或隐藏目录")
    resolved = path.expanduser().resolve()
    if "archive" in resolved.parts or not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("目录不在服务配置允许的项目范围内")
    if not resolved.is_dir():
        raise ValueError(f"目录不存在：{resolved}")
    return resolved


def list_folder(root: Path, relative: str, offset: int) -> dict:
    requested = root / relative
    directory = checked_directory(requested, (root,))
    if any(part.is_symlink() for part in (requested, *requested.parents) if part != root.parent):
        # Keep links from silently changing the selected project boundary.
        raise ValueError("请使用项目内的实际目录，不通过符号链接浏览")
    try:
        paths = sorted(
            (
                path
                for path in directory.iterdir()
                if path.name != "archive"
                and not path.name.startswith(".")
                and not path.is_symlink()
            ),
            key=lambda path: (not path.is_dir(), path.name),
        )
        entries = [
            {
                "name": path.name,
                "path": str(path.relative_to(root)),
                "absolute_path": str(path),
                "kind": "directory" if path.is_dir() else "file",
            }
            for path in paths[offset : offset + 100]
        ]
    except OSError as error:
        raise ValueError(f"无法读取目录：{directory}") from error
    return {
        "directory": str(directory),
        "relative_path": str(directory.relative_to(root)),
        "parent": str(directory.parent.relative_to(root)) if directory != root else None,
        "entries": entries,
        "next_offset": offset + 100 if len(paths) > offset + 100 else None,
    }
