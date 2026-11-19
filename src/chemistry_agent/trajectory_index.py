"""Index complete XYZ and VASP frames by byte offset in bounded batches."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from io import BytesIO
from pathlib import PurePosixPath
from typing import NamedTuple

_ATOM_COUNT = re.compile(rb"[0-9]{1,6}")
_VASP_MODE = re.compile(
    rb"(?:direct|cartesian|kartesian|d|c|k)(?:\s+configuration\s*=\s*(\d{1,16}))?",
    re.IGNORECASE,
)
VASP_HEADER_BYTES = 64 * 1024


class VaspHeader(NamedTuple):
    start: int
    end: int
    atoms: int


class IncompleteVaspFrame(ValueError):
    pass


def _vasp_header(reader: BytesIO, offset: int) -> VaspHeader:
    """Read a VASP 5/6 header, leaving its coordinate mode to the frame scanner."""
    start = reader.tell()
    rows = [reader.readline() for _ in range(7)]
    if not rows[-1].endswith(b"\n"):
        raise IncompleteVaspFrame("VASP 晶胞头不完整。")
    try:
        counts = [int(word) for word in rows[-1].split()]
    except ValueError as exc:
        raise ValueError("VASP 元素计数无效。") from exc
    atoms = sum(counts)
    if not counts or min(counts) < 0 or not 1 <= atoms <= 2**53 - 1:
        raise ValueError("VASP 元素计数无效。")
    mode_at = reader.tell()
    mode = reader.readline()
    if mode.lstrip().lower().startswith(b"s"):
        mode_at = reader.tell()
        mode = reader.readline()
    if not mode.endswith(b"\n"):
        raise IncompleteVaspFrame("VASP 晶胞头不完整。")
    if not _VASP_MODE.fullmatch(mode.strip()):
        raise ValueError("无法识别 VASP 坐标模式。")
    if mode_at - start >= VASP_HEADER_BYTES:
        raise ValueError("VASP 晶胞头超过 64 KiB。")
    reader.seek(mode_at)
    return VaspHeader(offset + start, offset + mode_at, atoms)


def vasp_header(data: bytes, offset: int) -> VaspHeader:
    return _vasp_header(BytesIO(data), offset)


def index_vasp(
    data: bytes, offset: int, size: int, *, first: bool, header: VaspHeader | None
) -> dict:
    """Return coordinate spans, header spans and step numbers for complete XDATCAR frames."""
    eof = offset + len(data) == size
    if not eof and not data.endswith(b"\n"):
        data = data[: data.rfind(b"\n") + 1]
    stream = BytesIO(data)
    spans = []
    text = None
    # On a partial frame, resume at its mode or the start of its new cell header.
    cursor = offset
    next_header = header.start if header else 0
    while len(spans) < 4096:
        start = stream.tell()
        mode = stream.readline()
        if not mode:
            cursor = size if eof else offset + start
            break
        # The title on the first VASP header may be blank.
        if header and not mode.strip():
            cursor = offset + stream.tell()
            continue
        try:
            match = _VASP_MODE.fullmatch(mode.strip())
            if header is None or match is None:
                stream.seek(start)
                header = _vasp_header(stream, offset)
                mode_at = stream.tell()
                match = _VASP_MODE.fullmatch(stream.readline().strip())
            else:
                mode_at = start
            for _ in range(header.atoms):
                row = stream.readline()
                # No terminal newline is valid when the final row has coordinates.
                if len(row.split(None, 3)) < 3:
                    if not row or not row.endswith(b"\n"):
                        raise IncompleteVaspFrame("VASP 轨迹末尾存在不完整帧。")
                    raise ValueError("VASP 结构帧坐标不完整。")
        except IncompleteVaspFrame:
            cursor = size if eof else offset + start
            break
        step = int(match[1]) if match and match[1] else None
        if step is not None and step > 2**53 - 1:
            raise ValueError("VASP 离子步编号超出有效范围。")
        cursor = offset + stream.tell()
        next_header = header.start
        spans.append((offset + mode_at, cursor, header.start, header.end, step))
        if first:
            text = (
                data[header.start - offset : header.end - offset] + data[mode_at : stream.tell()]
            ).decode("utf-8", errors="replace")
            break
    return {"spans": spans, "nextOffset": cursor, "headerOffset": next_header, "text": text}


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
    target, offset, length, size, first, *options = json.load(sys.stdin)
    vasp, header_offset = options or (False, 0)
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
        if vasp and offset:
            reader.seek(header_offset)
            header = vasp_header(
                reader.read(min(VASP_HEADER_BYTES, size - header_offset)), header_offset
            )
            if header.end > offset:
                raise ValueError("VASP 晶胞头偏移无效。")
        else:
            header = None
        deadline = time.monotonic() + 1
        for _ in range(8 if length >= 8 * 1024**2 else 1):
            reader.seek(cursor)
            data = reader.read(min(length, size - cursor))
            if len(data) != min(length, size - cursor):
                raise OSError("Trajectory was truncated")
            if vasp:
                batch = index_vasp(data, cursor, size, first=first, header=header)
                frames, following = batch["spans"], batch["nextOffset"]
                header_offset = batch["headerOffset"]
                first_frame = batch["text"]
                if frames and (header is None or header.start != header_offset):
                    reader.seek(header_offset)
                    header = vasp_header(
                        reader.read(min(VASP_HEADER_BYTES, size - header_offset)), header_offset
                    )
            else:
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
        payload = {"spans": spans, "nextOffset": cursor, "text": first_frame}
        if vasp:
            payload["headerOffset"] = header_offset
        print(json.dumps(payload))


if __name__ == "__main__":
    try:
        remote_index()
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
    except OSError:
        print(json.dumps({"changed": True}))
