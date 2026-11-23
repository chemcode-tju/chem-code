"""Bounded file IO for asset snapshots and portable checkout directories."""

import asyncio
import hashlib
import os
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from uuid import uuid4

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.connection import connection
from chemistry_agent.hosting.registry import Hosting

FILE_LIMIT = 8 * 1024 * 1024
TOTAL_LIMIT = 32 * 1024 * 1024


def asset_path(value: str, *, relative: bool) -> str:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute() != (not relative and not value.startswith("~/"))
        or relative
        and value.startswith("~")
        or any(part.casefold() == "archive" or part == ".." for part in path.parts)
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("使用绝对目录或 ~/，文件使用相对路径，路径不能包含归档或父目录跳转")
    return str(path)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def freeze(root: Path, content: bytes) -> str:
    """Publish complete immutable content before the manifest transaction commits."""
    key = digest(content)
    path = root / key
    root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=root) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        try:
            path.hardlink_to(temporary.name)
        except FileExistsError as exc:
            if path.is_symlink() or digest(path.read_bytes()) != key:
                raise AgentError("asset_integrity", "已有资产文件的校验和不匹配。", 409) from exc
    return key


class LocalFolder:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open(cls, directory: str) -> "LocalFolder":
        root = Path(directory).expanduser().resolve(strict=True)
        asset_path(str(root), relative=False)
        if not root.is_dir():
            raise NotADirectoryError(directory)
        return cls(root)

    def _read(self, name: str, limit: int, tail: bool | None) -> tuple[bytes, int] | None:
        try:
            candidate = self.root / name
            path = candidate.resolve(strict=True)
        except FileNotFoundError:
            return None
        if path != candidate or not path.is_file():
            raise AgentError("asset_file_type", "资产文件须为目录中的普通文件。", 422)
        with path.open("rb") as reader:
            before = os.fstat(reader.fileno())
            size = before.st_size
            if tail is None and size > limit:
                raise AgentError("asset_size", f"{name} 超过单文件快照限制。", 413)
            if tail:
                reader.seek(max(0, size - limit))
            content = reader.read(limit)
            after = os.fstat(reader.fileno())
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise AgentError("asset_source_changed", f"{name} 仍在变化，请稍后重试。", 409)
        return content, size

    async def read(
        self, name: str, limit: int, *, tail: bool | None = None
    ) -> tuple[bytes, int] | None:
        return await asyncio.to_thread(self._read, name, limit, tail)

    def _write(self, folder: str, files: dict[str, bytes]) -> str:
        target = self.root / folder
        staging = self.root / f".chem-asset-{uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            for name, content in files.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            # The random destination is private to this invocation.
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging)
            raise
        return str(target)

    async def write(self, folder: str, files: dict[str, bytes]) -> str:
        return await asyncio.to_thread(self._write, folder, files)


class SSHFolder:
    def __init__(self, sftp, root: PurePosixPath) -> None:
        self.sftp, self.root = sftp, root

    @classmethod
    async def open(cls, sftp, directory: str) -> "SSHFolder":
        path = PurePosixPath(directory)
        if directory.startswith("~/"):
            path = PurePosixPath(await sftp.realpath(".")) / directory[2:]
        root = PurePosixPath(asset_path(await sftp.realpath(str(path)), relative=False))
        if not stat.S_ISDIR((await sftp.stat(str(root))).permissions or 0):
            raise NotADirectoryError(directory)
        return cls(sftp, root)

    async def read(
        self, name: str, limit: int, *, tail: bool | None = None
    ) -> tuple[bytes, int] | None:
        from asyncssh import SFTPNoSuchFile, SFTPNoSuchPath

        path = str(self.root / name)
        try:
            resolved = await self.sftp.realpath(path)
            before = await self.sftp.lstat(path)
        except (SFTPNoSuchFile, SFTPNoSuchPath):
            return None
        if resolved != path or not stat.S_ISREG(before.permissions or 0):
            raise AgentError("asset_file_type", "资产文件须为目录中的普通文件。", 422)
        size = before.size
        if size is None or (tail is None and size > limit):
            raise AgentError("asset_size", f"{name} 超过单文件快照限制。", 413)
        async with self.sftp.open(path, "rb") as reader:
            content = await reader.read(limit, max(0, size - limit) if tail else 0)
            after = await reader.stat()
        if (after.size, after.mtime, after.mtime_ns) != (size, before.mtime, before.mtime_ns):
            raise AgentError("asset_source_changed", f"{name} 仍在变化，请稍后重试。", 409)
        return content, size

    async def write(self, folder: str, files: dict[str, bytes]) -> str:
        from asyncssh import SFTPAttrs

        target = str(self.root / folder)
        staging = self.root / f".chem-asset-{uuid4().hex}"
        await self.sftp.mkdir(str(staging), SFTPAttrs(permissions=0o700))
        try:
            for name, content in files.items():
                path = staging / name
                await self.sftp.makedirs(str(path.parent), exist_ok=True)
                async with self.sftp.open(str(path), "xb") as writer:
                    await writer.write(content)
            await self.sftp.rename(str(staging), target)
        except BaseException:
            await self.sftp.rmtree(str(staging), ignore_errors=True)
            raise
        return target


@asynccontextmanager
async def folder(
    hosting: Hosting, host: str, directory: str
) -> AsyncIterator[LocalFolder | SSHFolder]:
    directory = asset_path(directory, relative=False)
    try:
        if host == "local":
            yield await asyncio.to_thread(LocalFolder.open, directory)
            return
        async with asyncio.timeout(90):
            async with connection(hosting, host) as ssh, ssh.start_sftp_client() as sftp:
                yield await SSHFolder.open(sftp, directory)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise AgentError("asset_folder_missing", "资产目录不存在或无法访问。", 404) from exc
    except PermissionError as exc:
        raise AgentError("asset_permission", "无法读取源目录或写入目标目录。", 403) from exc
    except TimeoutError as exc:
        raise AgentError("asset_timeout", "资产文件传输超时。", 504) from exc
