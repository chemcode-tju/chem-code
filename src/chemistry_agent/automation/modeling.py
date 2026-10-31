"""Run generated ASE code in a deny-by-default macOS sandbox; never fall back to exec."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from chemistry_agent.errors import AgentError


def sandbox_profile(directory: Path) -> str:
    """Only the job directory is writable; user files, network and IPC are unavailable."""
    prefix = Path(sys.prefix).resolve()
    if prefix == Path.home() or prefix in Path.home().parents:
        raise AgentError("unsafe_python_prefix", "需要独立 Python 环境运行建模代码。", 503)
    readable = [
        str(prefix),
        str(directory.resolve()),
        "/System",
        "/usr/lib",
        "/usr/share",
        "/private/var/db/dyld",
        "/Library/Apple",
    ]
    rules = " ".join(f"(subpath {json.dumps(path)})" for path in readable)
    return f"""(version 1)
(deny default)
(allow file-read* {rules} (literal "/dev/null") (literal "/dev/urandom")
 (literal "/dev/random") (literal "/private/etc/localtime")
 (literal "/") (literal "/usr") (literal "/etc") (literal "/private/etc/passwd"))
(allow file-read-metadata)
(allow file-write* (subpath {json.dumps(str(directory.resolve()))}) (literal "/dev/null"))
(allow process-exec (literal {json.dumps(str(Path(sys.executable).resolve()))}))
(allow process-info*)
(allow sysctl-read)
"""


async def execute_model(directory: Path, code: str) -> str:
    """Bound runtime and outputs, strip credentials, and retain the exact source for review."""
    executable = Path("/usr/bin/sandbox-exec")
    (directory / "model.py").write_text(code)
    if sys.platform != "darwin" or not await asyncio.to_thread(executable.exists):
        raise AgentError(
            "modeling_sandbox_unavailable",
            "此环境没有受限建模执行器；代码已保留，可下载运行后上传结构。不会无隔离执行。",
            503,
        )
    (directory / "sandbox.sb").write_text(sandbox_profile(directory))
    # The launcher lives outside the writable job directory and is read through the Python prefix
    # only via -c; model code cannot alter the limits applied before it runs.
    launcher = (
        "import resource, runpy; "
        "resource.setrlimit(resource.RLIMIT_CPU, (20, 20)); "
        "resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152)); "
        "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64)); "
        "runpy.run_path('model.py', run_name='__main__')"
    )
    environment = {
        "PATH": str(Path(sys.executable).parent),
        "HOME": str(directory),
        "TMPDIR": str(directory),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "en_US.UTF-8",
    }
    log_path = directory / "execution.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "-f",
            str(directory / "sandbox.sb"),
            str(await asyncio.to_thread(Path(sys.executable).resolve)),
            "-I",
            "-B",
            "-c",
            launcher,
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AgentError("modeling_timeout", "建模超过 30 秒，已停止该建模进程。", 422) from exc
    if log_path.is_symlink():
        raise AgentError("invalid_model_output", "建模输出不能是符号链接。", 422)
    with log_path.open("rb") as stream:
        log_text = stream.read(16000).decode(errors="replace")
    if process.returncode:
        raise AgentError("modeling_failed", f"建模执行失败：\n{log_text[-8000:]}", 422)
    output = directory / "structure.extxyz"
    if output.is_symlink() or not output.is_file() or output.stat().st_size > 2_000_000:
        raise AgentError("invalid_model_output", "请生成不超过 2 MB 的 structure.extxyz。", 422)
    return output.read_text()
