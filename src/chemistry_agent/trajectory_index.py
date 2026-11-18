"""Index complete XYZ frames by byte offset, following Atom Lens' bounded scanner."""

import json
import os
import re
import stat
import sys
import time
from pathlib import PurePosixPath

_ATOM_COUNT = re.compile(rb"[0-9]{1,6}")


def index_xyz(
    data: bytes, offset: int, size: int, *, first: bool
) -> tuple[list[tuple[int, int]], int]:
    """Return frame spans and the next byte to scan. Ignore a partial frame at EOF."""
    end = offset + len(data)
    at_eof = end == size
    rows = data.split(b"\n")
    if not at_eof or not rows[-1]:
        rows.pop()
    spans = []
    row, cursor = 0, offset
    while row < len(rows):
        count = rows[row].strip()
        if not count:
            cursor += len(rows[row]) + 1
            row += 1
            continue
        if not _ATOM_COUNT.fullmatch(count) or not 1 <= int(count) <= 100_000:
            if at_eof and row == len(rows) - 1 and not data.endswith(b"\n"):
                return spans, size
            raise ValueError(f"字节 {cursor} 处的 XYZ 原子数无效。")
        following = row + int(count) + 2
        if following > len(rows):
            return spans, size if at_eof else cursor
        atoms = rows[row + 2 : following]
        if b"" in atoms or any(map(bytes.isspace, atoms)):
            raise ValueError(f"字节 {cursor} 处的 XYZ 原子坐标不完整。")
        if at_eof and following == len(rows) and not data.endswith(b"\n"):
            if len(atoms[-1].split()) < 4:
                return spans, size
        stop = min(cursor + sum(map(len, rows[row:following])) + following - row, size)
        spans.append((cursor, stop))
        row, cursor = following, stop
        if first or len(spans) == 4096:
            return spans, cursor
    return spans, size if at_eof else min(cursor, end)


def remote_index() -> None:
    """Read the validated SSH path without following newly substituted symlinks."""
    target, offset, length, size, first = json.load(sys.stdin)
    path = PurePosixPath(target)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = os.open("/", flags | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)

    with os.fdopen(descriptor, "rb") as reader:
        before = os.fstat(reader.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size < size:
            raise OSError("Trajectory was replaced or truncated")

        cursor = offset
        spans = []
        first_frame = None
        deadline = time.monotonic() + 1
        for _ in range(8 if length >= 8 * 1024**2 else 1):
            reader.seek(cursor)
            data = reader.read(min(length, size - cursor))
            if len(data) != min(length, size - cursor):
                raise OSError("Trajectory was truncated")
            frames, following = index_xyz(data, cursor, size, first=first)
            if first and frames:
                start, stop = frames[0]
                first_frame = data[start - cursor : stop - cursor].decode("utf-8")
            spans.extend(frames)
            if first or following == size or following == cursor or len(spans) >= 4096:
                cursor = following
                break
            cursor = following
            if time.monotonic() >= deadline:
                break

        after = os.fstat(reader.fileno())
        if after.st_size < size or (
            after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns
        ):
            raise OSError("Trajectory was edited during indexing")
        print(json.dumps({"spans": spans, "nextOffset": cursor, "text": first_frame}))


if __name__ == "__main__":
    try:
        remote_index()
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
    except OSError:
        print(json.dumps({"changed": True}))
