"""Read-only navigation of conversation workspaces and local SSH file systems."""

import asyncio
import base64
import codecs
import hashlib
import json
import os
import re
import shlex
import stat
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from chemistry_agent import trajectory_index
from chemistry_agent.attachments import owned_attachment
from chemistry_agent.automation.local_ssh import enabled_aliases
from chemistry_agent.automation.queue_remote import DUMP_SUFFIXES, JOB_ID, dump_companions
from chemistry_agent.automation.routes import local_service
from chemistry_agent.automation.ssh_profiles import load_profile, profile_path, shell_profiles
from chemistry_agent.environments.registry import Environments
from chemistry_agent.errors import AgentError
from chemistry_agent.trajectory_index import VASP_HEADER_BYTES, index_vasp, index_xyz, vasp_header
from chemistry_agent.workspace_files import INTERNAL_FILES, MAX_FILE_BYTES

router = APIRouter(prefix="/api/file-browser", tags=["file browser"])
PAGE_SIZE = 150
PREVIEW_BYTES = 256 * 1024
SEARCH_MATCHES = 200
SEARCH_DIRECTORIES = 300
TRAJECTORY_BLOCK = 8 * 1024 * 1024
TRAJECTORY_EXTENSIONS = {
    ".xyz",
    ".extxyz",
    ".traj",
    ".gro",
    ".lammpstrj",
    ".dump",
    ".xdatcar",
    ".vasp",
    ".poscar",
    ".contcar",
}
TRAJECTORY_NAME = re.compile(r"^(?:xdatcar|poscar|contcar|dump)(?:$|[._-])", re.IGNORECASE)

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ogv": "video/ogg",
}
# The SSH helper is shipped over the authenticated channel, without installing
# files on the remote host or placing user-supplied paths in shell arguments.
XDATCAR_NAME = re.compile(r"^(?:xdatcar)(?:$|[._-])|\.xdatcar$", re.IGNORECASE)
REMOTE_INDEX_COMMAND = shlex.join(
    ["python3", "-I", "-S", "-B", "-c", Path(trajectory_index.__file__).read_text(encoding="utf-8")]
)
HIDDEN_NAMES = INTERNAL_FILES | {"archive", "__pycache__", "node_modules"}


def visible_name(name: str) -> bool:
    return (
        not name.startswith(".")
        and name.casefold() not in HIDDEN_NAMES
        and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in name)
    )


def checked_path(name: str, *, absolute: bool = False) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() != absolute or any(
        not visible_name(part) for part in path.parts if part not in {"/", "//"}
    ):
        raise AgentError("browser_path_invalid", "无法访问此路径。", 403)
    return path


def file_path(source: str, name: str) -> PurePosixPath:
    return PurePosixPath(name) if source.startswith("attachment:") else checked_path(name)


def inside(root: PurePosixPath, path: PurePosixPath) -> None:
    if not path.is_relative_to(root):
        raise AgentError("browser_path_invalid", "无法访问此路径。", 403)
    checked_path(str(path.relative_to(root)))


class Filesystem(Protocol):
    async def root(self, path: str) -> PurePosixPath: ...

    async def entries(
        self, root: PurePosixPath, path: PurePosixPath, *, follow_links: bool = True
    ) -> list[dict]: ...

    async def read(
        self, root: PurePosixPath, path: PurePosixPath, limit: int, *, full: bool, offset: int = 0
    ) -> tuple[int, bytes]: ...

    async def trajectory(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        offset: int,
        limit: int,
        snapshot: str | None,
    ) -> tuple[int, str, bytes]: ...


@dataclass(frozen=True)
class TrajectoryVersion:
    size: int
    identity: tuple
    modified: tuple

    def token(self, snapshot: str | None, *, strict: bool) -> tuple[int, str]:
        identity = self.digest(self.identity)
        version = self.digest(self.modified)
        if snapshot is None:
            return self.size, f"{self.size}:{identity}:{version}"
        size, saved_identity, saved_version = snapshot.split(":")
        size = int(size)
        if (
            identity != saved_identity
            or self.size < size
            or ((strict or self.size == size) and (self.size != size or version != saved_version))
        ):
            raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
        return size, snapshot

    @staticmethod
    def digest(values: tuple) -> str:
        return hashlib.sha256(repr(values).encode()).hexdigest()[:24]


def read_limit(size: int, limit: int, full: bool) -> None:
    if full and size > limit:
        raise AgentError("browser_file_size", "文件超过 128 MiB。", 413)


def regular_file() -> AgentError:
    return AgentError("browser_not_file", "只能读取普通文件。", 422)


@contextmanager
def open_directory(root: Path, path: PurePosixPath) -> Iterator[int]:
    """Walk from an open root without following symlinks introduced after validation."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in path.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


class LocalFilesystem:
    def __init__(self, workspace: Path | None = None, *, follow_links: bool = True) -> None:
        self.workspace = workspace
        self.follow_links = follow_links and workspace is None

    async def root(self, path: str, *, allow_missing: bool = False) -> Path:
        if self.workspace is not None:
            if path or await asyncio.to_thread(self.workspace.is_symlink):
                raise AgentError("browser_path_invalid", "无法访问此路径。", 403)
            return self.workspace
        home = Path.home()
        requested = (
            str(home)
            if path in {"", "~"}
            else (str(home / path[2:]) if path.startswith("~/") else path)
        )
        checked_path(requested, absolute=True)
        resolved = await asyncio.to_thread(Path(requested).resolve, strict=not allow_missing)
        checked_path(str(resolved), absolute=True)
        return resolved

    def target(self, root: Path, path: PurePosixPath) -> Path:
        candidate = root / path
        # A session's live directory may be absent until its first run.
        if candidate == root and self.workspace is not None and not root.exists():
            return root
        target = candidate.resolve(strict=True)
        inside(root, target)
        if not self.follow_links and candidate != target:
            raise AgentError("browser_path_invalid", "无法访问此路径。", 403)
        return target

    def list_entries(
        self, root: Path, path: PurePosixPath, follow_links: bool, directories_only: bool
    ) -> list[dict]:
        target = self.target(root, path)
        if self.workspace == target and not target.exists():
            return []
        entries = []
        with (
            open_directory(root, target.relative_to(root)) as directory,
            os.scandir(directory) as children,
        ):
            for child in children:
                if not visible_name(child.name):
                    continue
                try:
                    if child.is_symlink():
                        if not self.follow_links or not follow_links:
                            continue
                        self.target(root, path / child.name)
                    if child.is_dir():
                        kind = "directory"
                    elif not directories_only and child.is_file():
                        kind = "file"
                    else:
                        continue
                    size = child.stat(follow_symlinks=True).st_size if kind == "file" else None
                except (OSError, RuntimeError, AgentError):
                    continue
                entries.append({"name": child.name, "kind": kind, "size": size})
        return entries

    async def entries(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        *,
        follow_links: bool = True,
        directories_only: bool = False,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self.list_entries, Path(root), path, follow_links, directories_only
        )

    async def mkdir(self, root: PurePosixPath, name: str) -> None:
        def create() -> None:
            with open_directory(Path(root), PurePosixPath()) as parent:
                os.mkdir(name, dir_fd=parent)

        await asyncio.to_thread(create)

    async def folder_exists(self, root: PurePosixPath) -> bool:
        try:
            info = await asyncio.to_thread(Path(root).stat)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            raise AgentError("directory_invalid", "请选择文件夹", 422)
        return True

    @contextmanager
    def open_file(self, root: Path, path: PurePosixPath) -> Iterator:
        target = self.target(root, path)
        if target == root:
            raise regular_file()
        with open_directory(root, target.parent.relative_to(root)) as directory:
            entry = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise regular_file()
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(target.name, flags, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as reader:
            metadata = os.fstat(reader.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise regular_file()
            yield reader, metadata

    def read_bytes(
        self, root: Path, path: PurePosixPath, limit: int, full: bool, offset: int
    ) -> tuple[int, bytes]:
        with self.open_file(root, path) as (reader, metadata):
            read_limit(metadata.st_size, limit, full)
            reader.seek(offset)
            data = reader.read(limit + int(full))
            read_limit(len(data), limit, full)
            return metadata.st_size, data

    async def read(
        self, root: PurePosixPath, path: PurePosixPath, limit: int, *, full: bool, offset: int = 0
    ) -> tuple[int, bytes]:
        return await asyncio.to_thread(self.read_bytes, Path(root), path, limit, full, offset)

    def trajectory_bytes(
        self, root: Path, path: PurePosixPath, offset: int, limit: int, snapshot: str | None
    ) -> tuple[int, str, bytes]:
        strict = path.suffix.lower() == ".traj"

        def version(info: os.stat_result) -> TrajectoryVersion:
            return TrajectoryVersion(info.st_size, (info.st_dev, info.st_ino), (info.st_mtime_ns,))

        with self.open_file(root, path) as (reader, metadata):
            size, token = version(metadata).token(snapshot, strict=strict)
            reader.seek(offset)
            data = reader.read(min(limit, max(0, size - offset)))
            version(os.fstat(reader.fileno())).token(token, strict=strict)
            return size, token, data

    async def trajectory(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        offset: int,
        limit: int,
        snapshot: str | None,
    ) -> tuple[int, str, bytes]:
        return await asyncio.to_thread(
            self.trajectory_bytes, Path(root), path, offset, limit, snapshot
        )


class AttachmentFilesystem(LocalFilesystem):
    """Use the original snapshot, whether it is stored on disk or in the legacy BLOB."""

    def __init__(self, file_id: UUID, original: Path | bytes) -> None:
        super().__init__(follow_links=False)
        self.file_id, self.original = file_id, original

    def open_file(self, root: Path, path: PurePosixPath):
        return super().open_file(root, PurePosixPath(str(self.file_id)))

    def read_bytes(
        self, root: Path, path: PurePosixPath, limit: int, full: bool, offset: int
    ) -> tuple[int, bytes]:
        if isinstance(self.original, Path):
            return super().read_bytes(root, path, limit, full, offset)
        size = len(self.original)
        read_limit(size, limit, full)
        return size, self.original[offset : offset + limit]

    def trajectory_bytes(
        self, root: Path, path: PurePosixPath, offset: int, limit: int, snapshot: str | None
    ) -> tuple[int, str, bytes]:
        if isinstance(self.original, Path):
            return super().trajectory_bytes(root, path, offset, limit, snapshot)
        version = TrajectoryVersion(len(self.original), (self.file_id,), ())
        size, token = version.token(snapshot, strict=True)
        return size, token, self.original[offset : offset + limit]


class RemoteFilesystem:
    def __init__(self, connection, sftp, ssh, state: "BrowserConnection") -> None:
        self.connection, self.sftp, self.ssh = connection, sftp, ssh
        self.state = state
        self.follow_links = True

    async def root(self, path: str, *, allow_missing: bool = False) -> PurePosixPath:
        if path in {"", "~"} or path.startswith("~/"):
            if self.state.home is None:
                self.state.home = PurePosixPath(await self.sftp.realpath("."))
            if not path[2:]:
                return checked_path(str(self.state.home), absolute=True)
            path = str(self.state.home / path[2:])
        requested = checked_path(path, absolute=True)
        missing = []
        while True:
            try:
                root = checked_path(await self.sftp.realpath(requested), absolute=True)
                return root.joinpath(*reversed(missing))
            except (self.ssh.SFTPNoSuchFile, self.ssh.SFTPNoSuchPath):
                if not allow_missing or requested == requested.parent:
                    raise
                missing.append(requested.name)
                requested = requested.parent

    async def target(self, root: PurePosixPath, path: PurePosixPath) -> PurePosixPath:
        if not path.parts and self.follow_links:
            return root
        target = checked_path(await self.sftp.realpath(root / path), absolute=True)
        inside(root, target)
        if not self.follow_links and target != root / path:
            raise AgentError("browser_path_invalid", "无法访问此路径。", 403)
        return target

    async def entries(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        *,
        follow_links: bool = True,
        directories_only: bool = False,
    ) -> list[dict]:
        entries = []
        links = []

        def add(name: str, attrs) -> None:
            if attrs.type == self.ssh.FILEXFER_TYPE_DIRECTORY:
                entries.append({"name": name, "kind": "directory"})
            elif not directories_only and attrs.type == self.ssh.FILEXFER_TYPE_REGULAR:
                entries.append({"name": name, "kind": "file", "size": attrs.size})

        async def add_link(name: str) -> None:
            try:
                target = await self.target(root, path / name)
                attrs = await self.sftp.stat(target)
            except (
                self.ssh.SFTPNoSuchFile,
                self.ssh.SFTPNoSuchPath,
                self.ssh.SFTPPermissionDenied,
                AgentError,
            ):
                return
            add(name, attrs)

        try:
            directory = await self.target(root, path)
            async for child in self.sftp.scandir(directory):
                name, attrs = child.filename, child.attrs
                if not visible_name(name):
                    continue
                if attrs.type == self.ssh.FILEXFER_TYPE_SYMLINK:
                    if follow_links and self.follow_links:
                        links.append(name)
                else:
                    add(name, attrs)
            for start in range(0, len(links), 12):
                await asyncio.gather(*(add_link(name) for name in links[start : start + 12]))
        except (self.ssh.SFTPNoSuchFile, self.ssh.SFTPNoSuchPath) as exc:
            raise AgentError("browser_missing", "文件或目录不存在。", 404) from exc
        except self.ssh.SFTPNotADirectory as exc:
            raise AgentError("directory_invalid", "请选择文件夹", 422) from exc
        except self.ssh.SFTPPermissionDenied as exc:
            raise AgentError("browser_access_denied", "没有文件读取权限。", 403) from exc
        return entries

    async def mkdir(self, root: PurePosixPath, name: str) -> None:
        target = root / name
        try:
            await self.sftp.mkdir(target)
        except (self.ssh.SFTPFailure, self.ssh.SFTPFileAlreadyExists) as exc:
            if await self.sftp.lexists(target):
                raise FileExistsError(name) from exc
            raise

    async def folder_exists(self, root: PurePosixPath) -> bool:
        try:
            info = await self.sftp.stat(root)
        except (self.ssh.SFTPNoSuchFile, self.ssh.SFTPNoSuchPath):
            return False
        if info.type != self.ssh.FILEXFER_TYPE_DIRECTORY:
            raise AgentError("directory_invalid", "请选择文件夹", 422)
        return True

    async def job_root(self, job: "JobLocation") -> PurePosixPath:
        now = time.monotonic()
        cache = self.state.job_roots
        cached = cache.get(job)
        if cached and cached[0] > now:
            cache.move_to_end(job)
            return cached[1]
        result = await self.queue_request(job.payload() | {"action": "queue_root"})
        directory = checked_path(result["directory"], absolute=True)
        cache[job] = (time.monotonic() + 15, directory)
        cache.move_to_end(job)
        if len(cache) > 256:
            cache.popitem(last=False)
        return directory

    async def read(
        self, root: PurePosixPath, path: PurePosixPath, limit: int, *, full: bool, offset: int = 0
    ) -> tuple[int, bytes]:
        target = await self.target(root, path)
        attrs = await self.sftp.lstat(target)
        if attrs.type != self.ssh.FILEXFER_TYPE_REGULAR:
            raise regular_file()
        read_limit(attrs.size or 0, limit, full)
        count = limit + int(full)
        if attrs.size is not None:
            count = min(count, max(0, attrs.size - offset) + int(full))
        if not count:
            return attrs.size or 0, b""
        async with self.sftp.open(target, "rb") as reader:
            data = await reader.read(count, offset)
        read_limit(len(data), limit, full)
        return attrs.size if attrs.size is not None else len(data), data

    async def queue_request(self, payload: dict) -> dict:
        from chemistry_agent.automation.slurm_jobs import QUEUE_HELPER

        result = await self.connection.run(
            shlex.join(["python3", "-c", QUEUE_HELPER]),
            input=json.dumps(payload),
            timeout=30,
        )
        if result.exit_status != 0:
            raise AgentError("job_ssh_failed", "无法核验作业目录。", 503)
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise AgentError("job_remote_error", "远端作业目录响应格式无效。", 502) from exc
        if data.get("error"):
            raise AgentError("job_remote_error", data["error"], 422)
        return data

    @staticmethod
    def trajectory_version(target: PurePosixPath, attrs) -> TrajectoryVersion:
        if attrs.size is None:
            raise regular_file()
        return TrajectoryVersion(
            attrs.size, (str(target), attrs.crtime, attrs.crtime_ns), (attrs.mtime, attrs.mtime_ns)
        )

    async def frame_index(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        offset: int,
        limit: int,
        snapshot: str | None,
        first: bool,
        *,
        vasp: bool,
        header_offset: int,
    ) -> dict | None:
        target = await self.target(root, path)
        attrs = await self.sftp.lstat(target)
        if attrs.type != self.ssh.FILEXFER_TYPE_REGULAR:
            raise regular_file()
        size, token = self.trajectory_version(target, attrs).token(snapshot, strict=False)
        if offset > size:
            raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
        try:
            result = await self.connection.run(
                REMOTE_INDEX_COMMAND,
                input=json.dumps([str(target), offset, limit, size, first, vasp, header_offset]),
                timeout=30,
            )
        except self.ssh.ChannelOpenError:
            return None
        if result.exit_status != 0:
            # The file browser also works with SFTP-only accounts.
            return None
        try:
            batch = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        if batch.get("error"):
            raise AgentError("browser_trajectory_invalid", batch["error"], 422)
        if batch.get("changed"):
            raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
        attrs = await self.sftp.lstat(target)
        self.trajectory_version(target, attrs).token(token, strict=False)
        return batch | {"size": size, "snapshot": token}

    async def trajectory(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        offset: int,
        limit: int,
        snapshot: str | None,
    ) -> tuple[int, str, bytes]:
        target = await self.target(root, path)
        attrs = await self.sftp.lstat(target)
        if attrs.type != self.ssh.FILEXFER_TYPE_REGULAR:
            raise regular_file()
        strict = path.suffix.lower() == ".traj"
        size, token = self.trajectory_version(target, attrs).token(snapshot, strict=strict)
        count = min(limit, max(0, size - offset))
        if not count:
            return size, token, b""
        async with self.sftp.open(target, "rb", max_requests=64) as reader:
            data = await reader.read(count, offset)
            self.trajectory_version(target, await reader.stat()).token(token, strict=strict)
        return size, token, data


def configured_hosts(request: Request) -> dict[str, tuple[str, str | None]]:
    settings = request.app.state.settings
    root = settings.platform_path
    profiles = shell_profiles(root)
    records = Environments(root).records()
    claimed = {row.id for row in records} | {row.ssh_alias for row in records if row.ssh_alias}
    hosts = {
        alias: alias
        for alias in set(enabled_aliases(root, settings.allowed_ssh_hosts)) | set(profiles)
        if alias not in claimed
    }
    hosts.update(
        {
            row.id: row.ssh_alias or row.id
            for row in records
            if row.enabled and not row.removed and row.transport == "ssh"
        }
    )
    return {host: (alias, profiles.get(alias)) for host, alias in hosts.items()}


@dataclass
class BrowserConnection:
    task: asyncio.Task
    users: int = 0
    expiry: asyncio.TimerHandle | None = None
    home: PurePosixPath | None = None
    job_roots: OrderedDict["JobLocation", tuple[float, PurePosixPath]] = field(
        default_factory=OrderedDict
    )


class BrowserConnections:
    """Share one authenticated SFTP channel per host among concurrent reads."""

    def __init__(self, platform_path: Path) -> None:
        self.platform_path = platform_path
        self.connections: dict[tuple, BrowserConnection] = {}

    def connection_key(self, host: str, profile: str | None) -> tuple:
        files = [Path.home() / ".ssh/config", Path.home() / ".ssh/known_hosts"]
        if profile:
            files.append(profile_path(self.platform_path, profile))
        revisions = []
        for path in files:
            try:
                metadata = path.stat()
                revisions.append((metadata.st_ino, metadata.st_mtime_ns, metadata.st_size))
            except FileNotFoundError:
                revisions.append(None)
        return (host, profile, *revisions)

    async def connect(self, ssh, host: str, profile: str | None) -> tuple:
        from chemistry_agent.automation.models import ServerCreate
        from chemistry_agent.automation.ssh_connection import connect, resolve

        async with asyncio.timeout(25):
            if profile:
                record = await asyncio.to_thread(load_profile, self.platform_path, profile)
                connection = await connect(self.platform_path, record)
            else:
                server = ServerCreate(label=host, hostname=host, workdir="/tmp")
                target = await asyncio.to_thread(resolve, server)
                known_hosts = [str(path) for path in target.files if path.is_file()]
                connection = await ssh.connect(
                    host,
                    config=(),
                    port=target.port,
                    username=target.username,
                    known_hosts=known_hosts or b"",
                    host_key_alias=target.lookup,
                    agent_forwarding=False,
                    password_auth=False,
                    kbdint_auth=False,
                    connect_timeout=12,
                    login_timeout=15,
                )
            try:
                return connection, await connection.start_sftp_client()
            except BaseException:
                connection.close()
                raise

    def retire(self, key: tuple, entry: BrowserConnection) -> None:
        if self.connections.get(key) is entry:
            self.connections.pop(key)
        if entry.expiry:
            entry.expiry.cancel()
        if not entry.task.done():
            entry.task.cancel()
        elif not entry.task.cancelled() and entry.task.exception() is None:
            connection, _ = entry.task.result()
            connection.close()

    @asynccontextmanager
    async def use(self, ssh, host: str, profile: str | None) -> AsyncIterator[RemoteFilesystem]:
        key = await asyncio.to_thread(self.connection_key, host, profile)
        entry = self.connections.get(key)
        if (
            entry
            and entry.task.done()
            and (
                entry.task.cancelled()
                or entry.task.exception() is not None
                or entry.task.result()[0].is_closed()
            )
        ):
            self.retire(key, entry)
            entry = None
        if entry is None:
            entry = BrowserConnection(asyncio.create_task(self.connect(ssh, host, profile)))
            self.connections[key] = entry
        if entry.expiry:
            entry.expiry.cancel()
        entry.users += 1
        try:
            connection, sftp = await asyncio.shield(entry.task)
            yield RemoteFilesystem(connection, sftp, ssh, entry)
        except (ssh.ConnectionLost, ssh.SFTPConnectionLost, ssh.SFTPNoConnection):
            self.retire(key, entry)
            raise
        finally:
            entry.users -= 1
            if not entry.users and self.connections.get(key) is entry:
                if not entry.task.done() or entry.task.cancelled() or entry.task.exception():
                    self.retire(key, entry)
                else:
                    entry.expiry = asyncio.get_running_loop().call_later(
                        90, self.retire, key, entry
                    )

    async def close(self) -> None:
        entries = list(self.connections.items())
        for key, entry in entries:
            self.retire(key, entry)
        results = await asyncio.gather(
            *(entry.task for _, entry in entries), return_exceptions=True
        )
        await asyncio.gather(
            *(result[0].wait_closed() for result in results if isinstance(result, tuple))
        )


@asynccontextmanager
async def remote_filesystem(
    request: Request, host: str, profile: str | None
) -> AsyncIterator[RemoteFilesystem]:
    try:
        import asyncssh
    except ImportError as exc:
        raise AgentError("browser_ssh_missing", "SSH 文件浏览需要 AsyncSSH。", 503) from exc
    try:
        async with asyncio.timeout(60):
            async with request.app.state.browser_connections.use(
                asyncssh, host, profile
            ) as backend:
                yield backend
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
        raise AgentError("browser_missing", "文件或目录不存在。", 404) from exc
    except (asyncssh.SFTPPermissionDenied, asyncssh.PermissionDenied) as exc:
        raise AgentError(
            "browser_access_denied", "无法访问，请检查 SSH 身份或文件权限。", 403
        ) from exc
    except asyncssh.HostKeyNotVerifiable as exc:
        raise AgentError(
            "browser_ssh_host_key", "主机密钥未获信任，请检查 SSH 设置。", 403
        ) from exc
    except TimeoutError as exc:
        raise AgentError("browser_timeout", "文件读取超时，请重试。", 504) from exc
    except (OSError, asyncssh.Error) as exc:
        raise AgentError(
            "browser_ssh_failed", "无法读取 SSH 文件，请检查连接和目录。", 502
        ) from exc


@dataclass(frozen=True)
class JobLocation:
    host: str
    job_id: str
    submitted: str
    history: bool

    @classmethod
    def from_request(cls, request: Request, host: str) -> "JobLocation":
        local_service(request)
        fields = request.query_params
        job_id, submitted = fields.get("job_id", ""), fields.get("submitted", "")
        if not re.fullmatch(JOB_ID, job_id) or len(job_id) > 64 or not 1 <= len(submitted) <= 80:
            raise AgentError("job_identity_invalid", "作业编号或提交时间无效。", 422)
        return cls(host, job_id, submitted, fields.get("history") == "true")

    def payload(self) -> dict:
        return {"job_id": self.job_id, "submitted": self.submitted, "history": self.history}


def job_scheduler(request: Request):
    local_service(request)
    return request.app.state.calculation_chat.plans.slurm


class RunnerFilesystem:
    """Use the same filesystem operations through the runner's authenticated transport."""

    def __init__(self, scheduler, job: JobLocation) -> None:
        self.scheduler, self.job = scheduler, job

    async def call(self, operation: str, path: PurePosixPath, **options) -> dict:
        return await self.scheduler.request(
            self.job.host,
            self.job.payload()
            | {
                "action": "queue_browse",
                "operation": operation,
                "path": "" if not path.parts else str(path),
                **options,
            },
        )

    async def root(self, path: str) -> PurePosixPath:
        return PurePosixPath()

    async def entries(
        self, root: PurePosixPath, path: PurePosixPath, *, follow_links: bool = True
    ) -> list[dict]:
        return (await self.call("directory", path, follow_links=follow_links))["entries"]

    async def read(
        self, root: PurePosixPath, path: PurePosixPath, limit: int, *, full: bool, offset: int = 0
    ) -> tuple[int, bytes]:
        response = await self.call(
            "read", path, offset=offset, limit=min(limit, TRAJECTORY_BLOCK), full=False
        )
        size = response["size"]
        read_limit(size, limit, full)
        data = bytearray(base64.b64decode(response["data"], validate=True))
        wanted = min(limit, max(0, size - offset))
        while len(data) < wanted:
            start = offset + len(data)
            _, _, chunk = await self.trajectory(
                root, path, start, min(wanted - len(data), TRAJECTORY_BLOCK), response["snapshot"]
            )
            if not chunk:
                raise AgentError("browser_changed", "文件已改变，请重新打开。", 409)
            data.extend(chunk)
        return size, bytes(data)

    async def trajectory(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        offset: int,
        limit: int,
        snapshot: str | None,
    ) -> tuple[int, str, bytes]:
        result = await self.call("range", path, offset=offset, limit=limit, snapshot=snapshot)
        return result["size"], result["snapshot"], base64.b64decode(result["data"], validate=True)


@asynccontextmanager
async def job_filesystem(request: Request, job: JobLocation):
    scheduler = job_scheduler(request)
    if job.host not in scheduler.hosts():
        raise AgentError("job_host_unknown", "请先登记该 SSH 服务器别名。", 422)
    environment = await asyncio.to_thread(Environments(scheduler.root).get, job.host)
    if environment and environment.transport == "runner":
        yield RunnerFilesystem(scheduler, job), PurePosixPath()
        return
    if environment and environment.transport == "local":
        result = await scheduler.request(job.host, job.payload() | {"action": "queue_root"})
        backend = LocalFilesystem(follow_links=False)
        yield backend, await backend.root(result["directory"])
        return
    async with configured_ssh_filesystem(request, job.host) as backend:
        directory = await backend.job_root(job)
        backend.follow_links = False
        yield backend, directory


async def job_file_operation(request: Request, host: str, payload: dict) -> dict:
    scheduler = job_scheduler(request)
    if host not in scheduler.hosts():
        raise AgentError("job_host_unknown", "请先登记该 SSH 服务器别名。", 422)
    environment = await asyncio.to_thread(Environments(scheduler.root).get, host)
    if environment and environment.transport != "ssh":
        return await scheduler.request(host, payload)
    async with configured_ssh_filesystem(request, host) as backend:
        return await backend.queue_request(payload)


@contextmanager
def browser_errors() -> Iterator[None]:
    try:
        yield
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise AgentError("browser_missing", "文件或目录不存在。", 404) from exc
    except PermissionError as exc:
        raise AgentError("browser_access_denied", "没有文件读取权限。", 403) from exc
    except (OSError, RuntimeError) as exc:
        raise AgentError("browser_read_failed", "无法读取文件或目录。", 422) from exc


@asynccontextmanager
async def resolved_filesystem(request: Request, source: str, session: UUID | None, root: str):
    with browser_errors():
        if source.startswith("job:"):
            job = JobLocation.from_request(request, source.removeprefix("job:"))
            async with job_filesystem(request, job) as location:
                yield location
            return
        async with filesystem(request, source, session) as backend:
            yield backend, await backend.root(root)


@asynccontextmanager
async def resolved_file(
    request: Request, source: str, session: UUID | None, root: str, path: PurePosixPath
):
    if source.startswith("attachment:"):
        try:
            file_id = UUID(source.removeprefix("attachment:"))
        except ValueError as exc:
            raise AgentError("file_not_found", "文件不存在。", 404) from exc
        name, original = await owned_attachment(file_id, request)
        if path.parts != (name,):
            raise AgentError("file_not_found", "文件不存在。", 404)
        base = original.parent if isinstance(original, Path) else Path()
        with browser_errors():
            yield AttachmentFilesystem(file_id, original), base
        return
    async with resolved_filesystem(request, source, session, root) as location:
        yield location


@asynccontextmanager
async def configured_ssh_filesystem(request: Request, host: str):
    hosts = await asyncio.to_thread(configured_hosts, request)
    if host not in hosts:
        raise AgentError("browser_host_denied", "请先在服务器设置中启用该连接。", 403)
    async with remote_filesystem(request, *hosts[host]) as remote:
        yield remote


def sort_entries(entries: list[dict]) -> None:
    entries.sort(
        key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"])
    )


@asynccontextmanager
async def picker_filesystem(request: Request, host: str):
    local_service(request)
    if host == "local":
        yield LocalFilesystem()
    else:
        async with configured_ssh_filesystem(request, host) as backend:
            yield backend


async def list_folders(request: Request, host: str, path: str, *, children: bool = True) -> dict:
    with browser_errors():
        async with picker_filesystem(request, host) as backend:
            root = await backend.root(path, allow_missing=True)
            if not children:
                return {"path": str(root), "exists": await backend.folder_exists(root)}
            exists = True
            try:
                entries = await backend.entries(
                    root, PurePosixPath(), follow_links=False, directories_only=True
                )
            except FileNotFoundError:
                entries, exists = [], False
            except NotADirectoryError as exc:
                raise AgentError("directory_invalid", "请选择文件夹", 422) from exc
            except AgentError as exc:
                if exc.code != "browser_missing":
                    raise
                entries, exists = [], False
    sort_entries(entries)
    return {
        "path": str(root),
        "parent": str(root.parent),
        "exists": exists,
        "directories": [
            {"name": item["name"], "path": str(root / item["name"])} for item in entries[:200]
        ],
        "truncated": len(entries) > 200,
    }


async def create_folder(request: Request, host: str, parent: str, name: str) -> dict:
    if not name or not visible_name(name) or "/" in name or "\\" in name:
        raise AgentError("directory_invalid", "请输入单个文件夹名称", 422)
    try:
        with browser_errors():
            async with picker_filesystem(request, host) as backend:
                root = await backend.root(parent)
                try:
                    await backend.mkdir(root, name)
                except FileExistsError as exc:
                    raise AgentError(
                        "directory_exists", "此处已存在同名文件或文件夹。", 409
                    ) from exc
    except AgentError as exc:
        if exc.code != "browser_missing":
            raise
        raise AgentError("directory_invalid", "请先打开父文件夹。", 422) from exc
    return {"path": str(root / name)}


@asynccontextmanager
async def filesystem(
    request: Request, source: str, session: UUID | None
) -> AsyncIterator[Filesystem]:
    if source == "workspace":
        if session is None:
            raise AgentError("browser_workspace_required", "请先打开一个对话。", 422)
        await request.app.state.store.require_session(str(session), request.state.user_id)
        backend = LocalFilesystem(request.app.state.workspace_files.workspace(str(session)))
    else:
        local_service(request)
        if source.startswith("ssh:"):
            async with configured_ssh_filesystem(request, source.removeprefix("ssh:")) as remote:
                yield remote
            return
        if source != "local":
            raise AgentError("browser_source_invalid", "文件位置不存在。", 404)
        backend = LocalFilesystem()
    yield backend


@router.get("/sources")
async def sources(request: Request) -> dict:
    locations = [{"id": "workspace", "label": "对话工作目录"}]
    try:
        local_service(request)
    except AgentError as exc:
        if exc.code != "automation_local_only":
            raise
    else:
        locations.append({"id": "local", "label": "本机"})
        for host in sorted(await asyncio.to_thread(configured_hosts, request)):
            locations.append({"id": f"ssh:{host}", "label": host})
    return {"sources": locations}


@router.get("/directory")
async def directory(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(default="", max_length=4096),
    offset: int = Query(default=0, ge=0),
) -> dict:
    relative = checked_path(path)
    async with resolved_filesystem(request, source, session, root) as (backend, base):
        entries = await backend.entries(base, relative)
    sort_entries(entries)
    end = offset + PAGE_SIZE
    if not source.startswith("job:"):
        entries = [{key: value for key, value in item.items() if key != "size"} for item in entries]
    return {
        "root": None if source == "workspace" else str(base),
        "entries": entries[offset:end],
        "next_offset": end if end < len(entries) else None,
    }


async def match_files(backend: Filesystem, root: PurePosixPath, query: str) -> dict:
    """Search names and relative paths without reading file contents or walking links."""
    pending = deque([PurePosixPath()])
    files = []
    scheduled = 1
    limited = False
    try:
        async with asyncio.timeout(6):
            while pending:
                directory = pending.popleft()
                try:
                    entries = await backend.entries(root, directory, follow_links=False)
                except (FileNotFoundError, NotADirectoryError, PermissionError, AgentError) as exc:
                    if not directory.parts or (
                        isinstance(exc, AgentError) and exc.status_code not in {403, 404}
                    ):
                        raise
                    continue
                for entry in sorted(entries, key=lambda item: item["name"].casefold()):
                    path = directory / entry["name"]
                    if entry["kind"] == "directory":
                        if scheduled < SEARCH_DIRECTORIES:
                            pending.append(path)
                            scheduled += 1
                        else:
                            limited = True
                    elif query in str(path).casefold():
                        files.append({"name": entry["name"], "kind": "file", "path": str(path)})
                        if len(files) >= SEARCH_MATCHES:
                            return {"files": files, "limited": True}
    except TimeoutError:
        limited = True
    return {"files": files, "limited": limited}


@router.get("/search")
async def search(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    q: str = Query(min_length=1, max_length=200),
) -> dict:
    async with resolved_filesystem(request, source, session, root) as (backend, base):
        if not q.strip():
            return {"files": [], "limited": False}
        return await match_files(backend, base, q.strip().casefold())


@router.get("/preview")
async def preview(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(max_length=4096),
) -> dict:
    relative = file_path(source, path)
    async with resolved_file(request, source, session, root, relative) as (backend, base):
        size, data = await backend.read(base, relative, PREVIEW_BYTES, full=False)
    result = {"kind": "binary", "size": size, "downloadable": size <= MAX_FILE_BYTES}
    truncated = size > len(data)
    encoding = "utf-8-sig"
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    try:
        text = codecs.getincrementaldecoder(encoding)().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return result
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return result
    return result | {"kind": "text", "text": text, "truncated": truncated}


@router.get("/companions")
async def companions(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(max_length=4096),
) -> dict:
    relative = checked_path(path)
    if relative.suffix.lower() not in DUMP_SUFFIXES:
        raise AgentError("browser_trajectory_type", "请选择 LAMMPS 文本轨迹。", 415)
    if source.startswith("job:"):
        job = JobLocation.from_request(request, source.removeprefix("job:"))
        return await job_file_operation(
            request, job.host, job.payload() | {"action": "queue_metadata", "path": str(relative)}
        )
    async with resolved_filesystem(request, source, session, root) as (backend, base):
        if isinstance(backend, RemoteFilesystem):
            target = await backend.target(base, relative)
            return await backend.queue_request(
                {"action": "browser_metadata", "directory": str(target.parent), "path": target.name}
            )
        target = await asyncio.to_thread(backend.target, base, relative)
        try:
            return {"companions": await asyncio.to_thread(dump_companions, target)}
        except ValueError as exc:
            raise AgentError("browser_trajectory_type", str(exc), 415) from exc


@router.get("/trajectory")
async def trajectory(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(max_length=4096),
    offset: int = Query(default=0, ge=0, le=2**53 - 1),
    length: int = Query(default=PREVIEW_BYTES, ge=1, le=TRAJECTORY_BLOCK),
    snapshot: str | None = Query(default=None, pattern=r"^[0-9]{1,16}:[a-f0-9]{24}:[a-f0-9]{24}$"),
    index: bool = False,
    first: bool = False,
    header_offset: int = Query(default=0, ge=0, le=2**53 - 1),
) -> Response:
    relative = file_path(source, path)
    suffix = relative.suffix.lower()
    vasp = bool(XDATCAR_NAME.search(relative.name))
    if suffix not in TRAJECTORY_EXTENSIONS and not TRAJECTORY_NAME.match(relative.name):
        raise AgentError("browser_trajectory_type", "此格式不支持分段预览。", 415)
    if index and suffix not in {".xyz", ".extxyz"} and not vasp:
        raise AgentError("browser_trajectory_type", "此格式不支持分段帧索引。", 415)
    if index and vasp and (header_offset > offset or (first and offset)):
        raise AgentError("browser_trajectory_invalid", "VASP 晶胞头或首帧偏移无效。", 422)
    if offset and not snapshot:
        raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
    async with resolved_file(request, source, session, root, relative) as (backend, base):
        header = None
        while True:
            batch = None
            if index and (vasp or not first) and isinstance(backend, RemoteFilesystem):
                batch = await backend.frame_index(
                    base,
                    relative,
                    offset,
                    length,
                    snapshot,
                    first,
                    vasp=vasp,
                    header_offset=header_offset,
                )
            if batch is None:
                size, snapshot, data = await backend.trajectory(
                    base, relative, offset, length, snapshot
                )
                if offset > size or len(data) != min(length, size - offset):
                    raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
                if not index:
                    break
                try:
                    if vasp:
                        if offset and header is None:
                            _, _, header_bytes = await backend.trajectory(
                                base, relative, header_offset, VASP_HEADER_BYTES, snapshot
                            )
                            header = vasp_header(header_bytes, header_offset)
                            if header.end > offset:
                                raise ValueError("VASP 晶胞头偏移无效。")
                        batch = await asyncio.to_thread(
                            index_vasp, data, offset, size, first=first, header=header
                        )
                    else:
                        spans, cursor = await asyncio.to_thread(
                            index_xyz, data, offset, size, first=first
                        )
                        batch = {"spans": spans, "nextOffset": cursor}
                        if first and spans:
                            text = data[spans[0][0] - offset : spans[0][1] - offset]
                            batch["text"] = text.decode("utf-8", errors="replace")
                except ValueError as exc:
                    raise AgentError("browser_trajectory_invalid", str(exc), 422) from exc
                batch |= {"size": size, "snapshot": snapshot}
            if batch["spans"] or batch["nextOffset"] == batch["size"]:
                if first and not batch["spans"]:
                    raise AgentError("browser_trajectory_invalid", "文件尚无完整结构帧。", 422)
                return JSONResponse(batch)
            if length == TRAJECTORY_BLOCK:
                if vasp:
                    # The browser's range scanner can continue across multiple blocks.
                    return JSONResponse(batch | {"rangeFallback": True})
                raise AgentError("browser_trajectory_frame", "单帧超过 8 MiB 分段预览上限。", 422)
            snapshot = batch["snapshot"]
            length = min(length * 4, TRAJECTORY_BLOCK)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "X-Trajectory-Size": str(size),
            "X-Trajectory-Snapshot": snapshot,
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/content")
async def content(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(max_length=4096),
) -> Response:
    relative = file_path(source, path)
    return await serve_file(request, source, session, root, relative, None)


@router.get("/media")
async def media(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(max_length=4096),
) -> Response:
    relative = file_path(source, path)
    media_type = MEDIA_TYPES.get(relative.suffix.lower())
    if media_type is None:
        raise AgentError("browser_media_type", "无法预览此媒体格式。", 415)
    return await serve_file(request, source, session, root, relative, media_type)


async def serve_file(
    request: Request,
    source: str,
    session: UUID | None,
    root: str,
    path: PurePosixPath,
    media_type: str | None,
) -> Response:
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}",
        "X-Content-Type-Options": "nosniff",
    }
    if media_type:
        headers["Accept-Ranges"] = "bytes"
    byte_range = request.headers.get("range") if media_type else None
    async with resolved_file(request, source, session, root, path) as (backend, base):
        if byte_range:
            match = re.fullmatch(r"bytes=([0-9]{1,12})-([0-9]{0,12})", byte_range)
            if not match:
                return Response(status_code=416, headers=headers)
            offset = int(match[1])
            end = min(int(match[2]), MAX_FILE_BYTES - 1) if match[2] else MAX_FILE_BYTES - 1
            if offset > end:
                return Response(status_code=416, headers=headers)
            size, data = await backend.read(base, path, end - offset + 1, full=False, offset=offset)
            read_limit(size, MAX_FILE_BYTES, True)
            if not data:
                headers["Content-Range"] = f"bytes */{size}"
                return Response(status_code=416, headers=headers)
            headers["Content-Range"] = f"bytes {offset}-{offset + len(data) - 1}/{size}"
            return Response(data, status_code=206, media_type=media_type, headers=headers)
        _, data = await backend.read(base, path, MAX_FILE_BYTES, full=True)
    return Response(data, media_type=media_type or "application/octet-stream", headers=headers)


async def read_job_on_runner(root: Path, payload: dict) -> dict:
    backend = LocalFilesystem(follow_links=False)
    base = await backend.root(str(root))
    path = checked_path(payload["path"])
    operation = payload["operation"]
    if operation == "directory":
        return {
            "entries": await backend.entries(base, path, follow_links=bool(payload["follow_links"]))
        }
    if operation not in {"read", "range"}:
        raise ValueError("不支持的文件读取操作。")
    offset, limit = payload["offset"], payload["limit"]
    if (
        not isinstance(offset, int)
        or not isinstance(limit, int)
        or offset < 0
        or not 1 <= limit <= TRAJECTORY_BLOCK
    ):
        raise ValueError("读取区间无效。")
    snapshot = payload.get("snapshot")
    if snapshot is not None and not re.fullmatch(
        r"[0-9]{1,16}:[a-f0-9]{24}:[a-f0-9]{24}", snapshot
    ):
        raise ValueError("轨迹版本无效。")
    size, token, data = await backend.trajectory(base, path, offset, limit, snapshot)
    return {"size": size, "snapshot": token, "data": base64.b64encode(data).decode()}
