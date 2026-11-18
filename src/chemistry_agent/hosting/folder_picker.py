"""Choose a directory from the desktop session of the platform Mac."""

import asyncio
import os
import sys
from pathlib import Path

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.remote_helper import location

_OSASCRIPT = Path("/usr/bin/osascript")
_PICKER = asyncio.Lock()
_SCRIPT = b"""on run argv
    set startFolder to (POSIX file (item 1 of argv)) as alias
    try
        activate
        set pickedFolder to choose folder default location startFolder
        return POSIX path of pickedFolder
    on error errorMessage number errorNumber
        if errorNumber is -128 then return ""
        error errorMessage number errorNumber
    end try
end run
"""


def desktop_picker_available() -> bool:
    """A service running outside the signed-in Mac user's session uses web browsing."""
    if sys.platform != "darwin":
        return False
    try:
        return os.getuid() not in (0, 65534) and Path("/dev/console").stat().st_uid == os.getuid()
    except OSError:
        return False


async def choose_folder(path: str) -> str | None:
    """Return a canonical path from the platform desktop or None on cancellation."""
    if not desktop_picker_available():
        raise AgentError("folder_picker_unavailable", "平台主机没有可用的 macOS 桌面。", 409)
    if _PICKER.locked():
        raise AgentError("folder_picker_busy", "平台主机已有一个文件夹选择窗口。", 409)
    async with _PICKER:
        process = None
        try:
            start = await asyncio.to_thread(location, path or "~/")
            if not await asyncio.to_thread(start.is_dir):
                start = Path.home()
            process = await asyncio.create_subprocess_exec(
                str(_OSASCRIPT),
                "-",
                str(start),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(180):
                stdout, _ = await process.communicate(_SCRIPT)
            if process.returncode:
                raise AgentError("folder_picker_failed", "系统文件夹选择器未能打开。", 502)
            output = stdout.decode().removesuffix("\n")
            if not output:
                return None
            selected = await asyncio.to_thread(location, output)
            if not await asyncio.to_thread(selected.is_dir):
                raise ValueError("请选择仍可访问的文件夹")
            return str(selected)
        except TimeoutError as exc:
            raise AgentError("folder_picker_timeout", "文件夹选择已超时。", 504) from exc
        except (OSError, ValueError) as exc:
            raise AgentError("folder_picker_failed", "无法使用所选文件夹。", 422) from exc
        finally:
            if process and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
