"""Shared native-session ownership, independent of Agent tools and HPC concurrency.

Inspired by Lody's ownership-before-start and instance-bound cleanup. No upstream
code is copied. A kernel lock, rather than an expiring timer, fences live writers.
"""

import asyncio
import fcntl
import json
import os
import re
import signal
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from chemistry_agent.errors import AgentError


@contextmanager
def delivery_attempt(progress) -> Iterator[None]:
    """Only failures before the native prompt boundary are safe to resend."""
    progress.native_turn_sent = False
    try:
        yield
    except BaseException as exc:
        if not progress.native_turn_sent:
            exc.delivery_not_sent = True
        raise


class NativeSessions:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def home(self, backend: str, session: str) -> Path:
        for identifier in (backend, session):
            if not re.fullmatch(r"[\w-]+", identifier):
                raise ValueError("Invalid native session identifier")
        return self.root / f"{backend}-state" / session

    def configure(self, transport, backend: str, session: str, prepare=None):
        transport.runtime_home = self.home(backend, session)
        transport.runtime_backend = backend
        transport.runtime_prepare = prepare
        return transport


class SessionLease:
    """The child inherits the lock: a dead host cannot admit a second live writer."""

    def __init__(self, home: Path, backend: str) -> None:
        self.home, self.backend = home, backend
        self.stream = None

    def acquire(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        stream = (self.home / "platform-writer.lock").open("a+")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise AgentError(
                f"{self.backend}_session_busy",
                "此会话正由另一平台进程处理，未重复发送。",
                409,
            ) from None
        self.stream = stream
        self.record()

    def record(self, child_pid: int | None = None) -> None:
        self.stream.seek(0)
        self.stream.truncate()
        json.dump({"host_pid": os.getpid(), "child_pid": child_pid}, self.stream)
        self.stream.flush()

    @property
    def descriptors(self) -> tuple[int, ...]:
        return (self.stream.fileno(),) if self.stream else ()

    def close(self) -> None:
        if self.stream:
            # Do not LOCK_UN: an orphaned child may still hold this file description.
            self.stream.close()
            self.stream = None


class ManagedTransport:
    """One lifecycle for chat, wakeups, probes, refresh and native compaction."""

    runtime_home: Path | None = None
    runtime_backend: str = "agent"
    runtime_prepare = None
    lease: SessionLease | None = None

    async def __aenter__(self):
        try:
            from chemistry_agent.hosting.native import current_native

            self.remote_native = current_native.get()
            if self.remote_native:
                # The remote bootstrap owns the private home and kernel writer lease.
                return await self.start()
            if self.runtime_home:
                self.lease = SessionLease(self.runtime_home, self.runtime_backend)
                self.lease.acquire()
            if self.runtime_prepare:
                preparation = asyncio.create_task(asyncio.to_thread(self.runtime_prepare))
                try:
                    await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    # Do not free the lock while a snapshot is still being written.
                    await preparation
                    raise
            return await self.start()
        except BaseException:
            await self.__aexit__()
            raise

    async def spawn(self, *arguments: str, **kwargs) -> None:
        if getattr(self, "remote_native", None):
            self.process = await self.remote_native.start(arguments)
            return
        self.process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
            start_new_session=True,
            pass_fds=self.lease.descriptors if self.lease else (),
            **kwargs,
        )
        if self.lease:
            self.lease.record(self.process.pid)

    async def _close(self) -> None:
        if getattr(self, "remote_native", None):
            if self.process:
                self.process.stdin.write_eof()
                try:
                    async with asyncio.timeout(6):
                        await self.process.wait_closed()
                except TimeoutError:
                    self.process.close()
            if self.reader:
                self.reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self.reader
            return
        # Also clean descendants when the group leader has already exited.
        if self.process:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(3):
                    await self.process.wait()
            except TimeoutError:
                pass
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
        if self.lease:
            self.lease.close()
            self.lease = None

    async def __aexit__(self, *args) -> None:
        # A second cancellation must not release ownership before process cleanup.
        cleanup = asyncio.create_task(self._close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
