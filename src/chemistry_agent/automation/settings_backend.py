"""Local AiiDA onboarding. Reading settings never bootstraps a missing profile."""

import json
import os
import subprocess
import sys
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

from filelock import FileLock

from chemistry_agent.automation.configuration import CalculationConfiguration
from chemistry_agent.errors import AgentError


def config_file(root: Path) -> Path:
    directory = root if root.name == ".aiida" else root / ".aiida"
    return directory / "config.json"


def profile_names(root: Path) -> set[str]:
    path = config_file(root)
    if not path.exists():
        return set()
    return set(json.loads(path.read_text())["profiles"])


def vasp_available() -> bool:
    return find_spec("aiida_vasp") is not None and bool(
        entry_points(group="aiida.calculations", name="vasp.vasp")
    )


def settings_status(profile: str, server_id: str | None = None) -> dict:
    root = Path(os.environ["AIIDA_PATH"])
    installed = find_spec("aiida") is not None
    plugin = vasp_available()
    profile_ready = installed and profile in profile_names(root)
    inventory = {"servers": [], "codes": [], "daemon_running": False}
    if profile_ready:
        from aiida import load_profile

        from chemistry_agent.automation import backend

        load_profile(profile)
        inventory = backend.status()
    configuration = CalculationConfiguration(root).read()
    servers = [s for s in inventory["servers"] if not server_id or str(s["id"]) == server_id]
    server_ids = {str(s["id"]) for s in servers}
    codes = [
        {k: v for k, v in code.items() if k != "environment"}
        for code in inventory["codes"]
        if str(code["server_id"]) in server_ids and code["plugin"] != "chemistry.structure_probe"
    ]
    scoped = {
        key: [entry for entry in entries if not server_id or entry["server_id"] == server_id]
        for key, entries in configuration.items()
    }
    default_id = CalculationConfiguration.default_id(scoped)
    sources = {s["id"]: s for s in configuration["sources"]}
    usable = [
        t
        for t in [*scoped["execution_targets"], *scoped["protocols"]]
        if t["server_id"] in server_ids
        and t["potential_source_id"] in sources
        and any(
            c["plugin"] == "vasp.vasp"
            and str(c["id"]) == t["code_id"]
            and str(c["server_id"]) == t["server_id"]
            for c in codes
        )
    ]
    missing = [
        name
        for name, available in (
            ("aiida_core", installed),
            ("vasp_plugin", plugin),
            ("profile", profile_ready),
            ("daemon", inventory["daemon_running"]),
            ("server", servers),
            ("software", any(c["plugin"] == "vasp.vasp" for c in codes)),
            ("potential_source", scoped["sources"]),
            ("execution_target", usable),
        )
        if not available
    ]
    return {
        "aiida": {
            "installed": installed,
            "vasp_plugin": plugin,
            "profile": profile,
            "profile_ready": profile_ready,
            "daemon_running": inventory["daemon_running"],
        },
        "servers": servers,
        "codes": codes,
        "potential_sources": scoped["sources"],
        "execution_targets": scoped["execution_targets"],
        "protocols": scoped["protocols"],
        "default_id": default_id,
        "missing": missing,
        "ready": not missing,
    }


def run_verdi(arguments: list[str]) -> None:
    try:
        subprocess.run(
            [str(Path(sys.executable).with_name("verdi")), *arguments],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(
            "aiida_setup_failed", "本地 AiiDA 初始化失败，请查看服务日志后重试。", 503
        ) from exc


def initialize(profile: str) -> dict:
    root = Path(os.environ["AIIDA_PATH"])
    path = config_file(root)
    if (
        not root.is_absolute()
        or ":" in str(root)
        or path.resolve() != root.resolve() / path.relative_to(root)
        or path.parent.resolve() == (Path.home() / ".aiida").resolve()
    ):
        raise AgentError("unsafe_aiida_path", "请为 Chem-Agent 指定独立的 AiiDA 配置目录。", 422)
    if find_spec("aiida") is None or not vasp_available():
        raise AgentError("aiida_missing", "请先安装项目的 automation 可选依赖。", 503)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "initialize.lock"), timeout=65):
        names = profile_names(root)
        if profile not in names:
            if names:
                raise AgentError(
                    "aiida_profile_conflict", "此目录已有其他 profile，请选用独立目录。", 409
                )
            run_verdi(
                [
                    "presto",
                    "--non-interactive",
                    "--use-zeromq",
                    "--profile-name",
                    profile,
                    "--email",
                    "local@chem-agent.invalid",
                ]
            )
        result = settings_status(profile)
        if not result["aiida"]["daemon_running"]:
            run_verdi(["-p", profile, "daemon", "start", "1"])
            result = settings_status(profile)
            if not result["aiida"]["daemon_running"]:
                raise AgentError(
                    "daemon_not_running", "本地 profile 已保留，daemon 未启动，请重试。", 503
                )
        return result
