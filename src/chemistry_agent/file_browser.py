"""Read-only navigation of conversation workspaces and local SSH file systems."""

import asyncio
import codecs
import hashlib
import json
import os
import re
import shlex
import stat
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from chemistry_agent import trajectory_index
from chemistry_agent.automation.local_ssh import enabled_aliases
from chemistry_agent.automation.routes import local_service
from chemistry_agent.automation.ssh_profiles import load_profile, profile_path, shell_profiles
from chemistry_agent.errors import AgentError
from chemistry_agent.trajectory_index import index_xyz
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
REMOTE_XYZ_COMMAND = shlex.join(
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
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace

    async def root(self, path: str) -> Path:
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
        resolved = await asyncio.to_thread(Path(requested).resolve, strict=True)
        checked_path(str(resolved), absolute=True)
        return resolved

    def target(self, root: Path, path: PurePosixPath) -> Path:
        candidate = root / path
        # A session's live directory may be absent until its first run.
        if candidate == root and self.workspace is not None and not root.exists():
            return root
        target = candidate.resolve(strict=True)
        inside(root, target)
        if self.workspace is not None and candidate != target:
            raise AgentError("browser_path_invalid", "无法访问此路径。", 403)
        return target

    def list_entries(self, root: Path, path: PurePosixPath, follow_links: bool) -> list[dict]:
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
                        if self.workspace is not None or not follow_links:
                            continue
                        self.target(root, path / child.name)
                    if child.is_dir():
                        kind = "directory"
                    elif child.is_file():
                        kind = "file"
                    else:
                        continue
                except (OSError, RuntimeError, AgentError):
                    continue
                entries.append({"name": child.name, "kind": kind})
        return entries

    async def entries(
        self, root: PurePosixPath, path: PurePosixPath, *, follow_links: bool = True
    ) -> list[dict]:
        return await asyncio.to_thread(self.list_entries, Path(root), path, follow_links)

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


class RemoteFilesystem:
    def __init__(self, connection, sftp, ssh) -> None:
        self.connection, self.sftp, self.ssh = connection, sftp, ssh

    async def root(self, path: str) -> PurePosixPath:
        if path in {"", "~"} or path.startswith("~/"):
            path = str(PurePosixPath(await self.sftp.realpath(".")) / path[2:])
        checked_path(path, absolute=True)
        return checked_path(await self.sftp.realpath(path), absolute=True)

    async def target(self, root: PurePosixPath, path: PurePosixPath) -> PurePosixPath:
        if not path.parts:
            return root
        target = checked_path(await self.sftp.realpath(root / path), absolute=True)
        inside(root, target)
        return target

    async def entries(
        self, root: PurePosixPath, path: PurePosixPath, *, follow_links: bool = True
    ) -> list[dict]:
        entries = []
        links = []

        def add(name: str, attrs) -> None:
            if attrs.type == self.ssh.FILEXFER_TYPE_DIRECTORY:
                entries.append({"name": name, "kind": "directory"})
            elif attrs.type == self.ssh.FILEXFER_TYPE_REGULAR:
                entries.append({"name": name, "kind": "file"})

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
                    if follow_links:
                        links.append(name)
                else:
                    add(name, attrs)
            for start in range(0, len(links), 12):
                await asyncio.gather(*(add_link(name) for name in links[start : start + 12]))
        except (self.ssh.SFTPNoSuchFile, self.ssh.SFTPNoSuchPath) as exc:
            raise AgentError("browser_missing", "文件或目录不存在。", 404) from exc
        except self.ssh.SFTPPermissionDenied as exc:
            raise AgentError("browser_access_denied", "没有文件读取权限。", 403) from exc
        return entries

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

    @staticmethod
    def trajectory_version(target: PurePosixPath, attrs) -> TrajectoryVersion:
        if attrs.size is None:
            raise regular_file()
        return TrajectoryVersion(
            attrs.size, (str(target), attrs.crtime, attrs.crtime_ns), (attrs.mtime, attrs.mtime_ns)
        )

    async def xyz_index(
        self,
        root: PurePosixPath,
        path: PurePosixPath,
        offset: int,
        limit: int,
        snapshot: str | None,
        first: bool,
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
                REMOTE_XYZ_COMMAND,
                input=json.dumps([str(target), offset, limit, size, first]),
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


def configured_hosts(request: Request) -> dict[str, str | None]:
    settings = request.app.state.settings
    root = settings.platform_path if settings.automation_enabled else None
    profiles = shell_profiles(root) if root else {}
    return dict.fromkeys(enabled_aliases(root, settings.allowed_ssh_hosts)) | profiles


@dataclass
class BrowserConnection:
    task: asyncio.Task
    users: int = 0
    expiry: asyncio.TimerHandle | None = None


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
            yield RemoteFilesystem(connection, sftp, ssh)
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
            host = source.removeprefix("ssh:")
            hosts = await asyncio.to_thread(configured_hosts, request)
            if host not in hosts:
                raise AgentError("browser_host_denied", "请先在服务器设置中启用该连接。", 403)
            async with remote_filesystem(request, host, hosts[host]) as remote:
                yield remote
            return
        if source != "local":
            raise AgentError("browser_source_invalid", "文件位置不存在。", 404)
        backend = LocalFilesystem()
    try:
        yield backend
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise AgentError("browser_missing", "文件或目录不存在。", 404) from exc
    except PermissionError as exc:
        raise AgentError("browser_access_denied", "没有文件读取权限。", 403) from exc
    except (OSError, RuntimeError) as exc:
        raise AgentError("browser_read_failed", "无法读取文件或目录。", 422) from exc


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
    async with filesystem(request, source, session) as backend:
        base = await backend.root(root)
        entries = await backend.entries(base, relative)
    entries.sort(
        key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"])
    )
    end = offset + PAGE_SIZE
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
                        files.append(entry | {"path": str(path)})
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
    async with filesystem(request, source, session) as backend:
        base = await backend.root(root)
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
    relative = checked_path(path)
    async with filesystem(request, source, session) as backend:
        base = await backend.root(root)
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
) -> Response:
    relative = checked_path(path)
    suffix = relative.suffix.lower()
    if suffix not in TRAJECTORY_EXTENSIONS and not TRAJECTORY_NAME.match(relative.name):
        raise AgentError("browser_trajectory_type", "此格式不支持分段预览。", 415)
    if index and suffix not in {".xyz", ".extxyz"}:
        raise AgentError("browser_trajectory_type", "此格式不支持 XYZ 帧索引。", 415)
    if offset and not snapshot:
        raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
    async with filesystem(request, source, session) as backend:
        base = await backend.root(root)
        while True:
            batch = None
            if index and not first and isinstance(backend, RemoteFilesystem):
                batch = await backend.xyz_index(base, relative, offset, length, snapshot, first)
            if batch is None:
                size, snapshot, data = await backend.trajectory(
                    base, relative, offset, length, snapshot
                )
                if offset > size or len(data) != min(length, size - offset):
                    raise AgentError("browser_changed", "轨迹文件已改变，请重新打开。", 409)
                if not index:
                    break
                try:
                    spans, cursor = await asyncio.to_thread(
                        index_xyz, data, offset, size, first=first
                    )
                except ValueError as exc:
                    raise AgentError("browser_trajectory_invalid", str(exc), 422) from exc
                batch = {"size": size, "snapshot": snapshot, "spans": spans, "nextOffset": cursor}
                if first and spans:
                    text = data[spans[0][0] - offset : spans[0][1] - offset]
                    batch["text"] = text.decode("utf-8", errors="replace")
            if batch["spans"] or batch["nextOffset"] == batch["size"]:
                if first and not batch["spans"]:
                    raise AgentError("browser_trajectory_invalid", "文件尚无完整结构帧。", 422)
                return JSONResponse(batch)
            if length == TRAJECTORY_BLOCK:
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
    relative = checked_path(path)
    return await serve_file(request, source, session, root, relative, None)


@router.get("/media")
async def media(
    request: Request,
    source: str = Query(max_length=200),
    session: UUID | None = None,
    root: str = Query(default="", max_length=4096),
    path: str = Query(max_length=4096),
) -> Response:
    relative = checked_path(path)
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
    async with filesystem(request, source, session) as backend:
        base = await backend.root(root)
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
