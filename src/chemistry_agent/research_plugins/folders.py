"""Bounded directory navigation; file content is opened by Codex/Atom Lens."""

from pathlib import Path


def checked_path(path: Path, roots: tuple[Path, ...]) -> Path:
    """Resolve links while keeping navigation inside the registered project."""
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise ValueError(f"无法解析路径：{path}") from error
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("路径不在服务配置允许的项目范围内")
    return resolved


def checked_directory(path: Path, roots: tuple[Path, ...]) -> Path:
    resolved = checked_path(path, roots)
    if not resolved.is_dir():
        raise ValueError(f"目录不存在：{resolved}")
    return resolved


def folder_entry(path: Path, root: Path) -> dict:
    """Include every entry, even when its link target cannot be opened."""
    entry = {
        "name": path.name,
        "path": str(path.relative_to(root)),
        "absolute_path": str(path),
        "kind": "file",
        "is_symlink": False,
        "open_error": None,
    }
    try:
        entry["is_symlink"] = path.is_symlink()
        entry["kind"] = "directory" if path.is_dir() else "file"
        resolved = checked_path(path, (root,))
        if not resolved.exists():
            raise ValueError("链接目标不存在" if entry["is_symlink"] else "文件已不存在")
        entry["absolute_path"] = str(resolved)
    except (OSError, ValueError) as error:
        entry["open_error"] = str(error)
    return entry


def list_folder(root: Path, relative: str, offset: int) -> dict:
    directory = checked_directory(root / relative, (root,))
    try:
        entries = sorted(
            (folder_entry(path, root) for path in directory.iterdir()),
            key=lambda entry: (entry["kind"] != "directory", entry["name"]),
        )
    except OSError as error:
        raise ValueError(f"无法读取目录：{directory}") from error
    return {
        "directory": str(directory),
        "relative_path": str(directory.relative_to(root)),
        "parent": str(directory.parent.relative_to(root)) if directory != root else None,
        "entries": entries[offset : offset + 100],
        "next_offset": offset + 100 if len(entries) > offset + 100 else None,
    }
