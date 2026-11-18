"""Central, immutable Agent Skills packages; compatible with Anthropic marketplaces.

Installation copies data only. It never runs a package's scripts or install hooks.
Skills retain their scripts, references and licenses, and are loaded on demand.
"""

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

import yaml
from filelock import FileLock
from pydantic import BaseModel, Field

from chemistry_agent.automation.ssh_profiles import write_private
from chemistry_agent.errors import AgentError
from chemistry_agent.knowledge.openviking import relative_path

OFFICIAL_SOURCES = [
    {
        "name": "Anthropic Skills",
        "source": "https://github.com/anthropics/skills",
        "publisher": "Anthropic",
    },
    {"name": "OpenAI Skills", "source": "https://github.com/openai/skills", "publisher": "OpenAI"},
]


class PluginInstall(BaseModel):
    source: str = Field(min_length=1, max_length=2048)
    ref: str = Field(default="HEAD", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    plugin: str | None = None


class SkillQuery(BaseModel):
    query: str = ""


class SkillRead(BaseModel):
    skill: str
    path: str = "SKILL.md"
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=16000, ge=1, le=100000)


def files(root: Path):
    """Never traverse archives, Git internals or symlinks, including directory links."""
    for item in sorted(root.iterdir()):
        if item.name in {"archive", ".git"} or item.is_symlink():
            continue
        if item.is_dir():
            yield from files(item)
        elif item.is_file():
            yield item


def skill_metadata(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"Missing skill frontmatter: {path.name}")
    parts = text.split("---", 2)
    metadata = yaml.safe_load(parts[1])
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not isinstance(metadata.get("description"), str)
    ):
        raise ValueError("Skill name and description are required")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"]):
        raise ValueError("Invalid Agent Skill name")
    return {
        "name": metadata["name"],
        "description": metadata["description"],
        "compatibility": str(metadata.get("compatibility", "")),
    }


class SkillPlugins:
    def __init__(self, root: Path) -> None:
        self.root = root / "knowledge" / "plugins"
        self.index = self.root / "installed.json"

    def packages(self) -> list[dict]:
        return json.loads(self.index.read_text()).get("packages", []) if self.index.exists() else []

    def catalog(self, query: str = "") -> dict:
        result = []
        for package in self.packages():
            if not package["enabled"]:
                continue
            for skill in package["skills"]:
                if (
                    query
                    and query.casefold() not in json.dumps(skill, ensure_ascii=False).casefold()
                ):
                    continue
                result.append(
                    skill
                    | {"id": f"{package['id']}/{skill['key']}", "revision": package["revision"]}
                )
        return {"skills": result}

    async def install(self, request: PluginInstall) -> dict:
        with tempfile.TemporaryDirectory(prefix="chem-skills-") as temporary:
            source = await asyncio.to_thread(Path(request.source).expanduser)
            revision = "local"
            if request.source.startswith("https://"):
                if not re.fullmatch(
                    r"https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?/?", request.source
                ):
                    raise AgentError(
                        "plugin_source_invalid", "请提供 GitHub 仓库地址或本地目录。", 422
                    )
                source = Path(temporary) / "source"
                await self.git(
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--depth",
                    "1",
                    "--",
                    request.source,
                    str(source),
                )
                if request.ref != "HEAD":
                    await self.git(
                        "-C", str(source), "fetch", "--quiet", "--depth", "1", "origin", request.ref
                    )
                    await self.git(
                        "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"
                    )
                else:
                    await self.git("-C", str(source), "checkout", "--quiet", "--detach", "HEAD")
                revision = (await self.git("-C", str(source), "rev-parse", "HEAD")).strip()
            if not source.is_dir() or "archive" in source.resolve().parts:
                raise AgentError("plugin_source_invalid", "插件目录不存在。", 422)
            return await asyncio.to_thread(self._install, source, request, revision)

    @staticmethod
    async def git(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.file.allow=never",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(120):
                stdout, _ = await process.communicate()
            if process.returncode:
                raise AgentError("plugin_download_failed", "插件下载失败，请检查地址和版本。", 502)
            return stdout.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def _install(self, source: Path, request: PluginInstall, revision: str) -> dict:
        manifest = source / ".claude-plugin" / "marketplace.json"
        selected = None
        if request.plugin:
            if not manifest.is_file() or manifest.is_symlink() or manifest.parent.is_symlink():
                raise AgentError("plugin_manifest_missing", "此仓库没有插件市场清单。", 422)
            try:
                selected = next(
                    (
                        p
                        for p in json.loads(manifest.read_text())["plugins"]
                        if p["name"] == request.plugin
                    ),
                    None,
                )
                if selected and not isinstance(selected.get("skills"), list):
                    raise ValueError("Missing local skills list")
            except (ValueError, KeyError, TypeError) as exc:
                raise AgentError(
                    "plugin_manifest_invalid", "插件清单需要有效的本地 Skills 列表。", 422
                ) from exc
            if selected is None:
                raise AgentError("plugin_not_found", "插件名称不在市场清单中。", 404)
        roots = [source / relative_path(p) for p in selected["skills"]] if selected else [source]
        candidates = []
        for root in roots:
            if (
                not root.is_dir()
                or root.is_symlink()
                or not root.resolve().is_relative_to(source.resolve())
            ):
                raise AgentError("plugin_path_invalid", "插件路径越过包目录。", 422)
            candidates.extend(p for p in files(root) if p.name == "SKILL.md")
        skills, included = [], {}
        digest = hashlib.sha256()
        for path in sorted(set(candidates)):
            try:
                metadata = skill_metadata(path)
            except (ValueError, yaml.YAMLError) as exc:
                raise AgentError("skill_invalid", str(exc), 422) from exc
            key = hashlib.sha256(str(path.relative_to(source)).encode()).hexdigest()[:12]
            skills.append(
                metadata | {"key": key, "directory": str(path.parent.relative_to(source))}
            )
            for item in files(path.parent):
                included[str(item.relative_to(source))] = item
        for item in source.iterdir():
            if (
                item.is_file()
                and not item.is_symlink()
                and (item.name.upper().startswith(("LICENSE", "NOTICE", "COPYING")))
            ):
                included[item.name] = item
        if not skills:
            raise AgentError("skills_missing", "没有找到有效的 SKILL.md。", 422)
        if sum(p.stat().st_size for p in included.values()) > 100 * 1024 * 1024:
            raise AgentError("plugin_too_large", "插件超过 100 MiB，请选择单个插件包。", 422)
        for name, path in sorted(included.items()):
            digest.update(name.encode() + b"\0" + path.read_bytes())
        checksum = digest.hexdigest()
        identifier = hashlib.sha256(
            f"{request.source}:{request.plugin}:{checksum}".encode()
        ).hexdigest()[:24]
        package_name = request.source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        record = {
            "id": identifier,
            "name": request.plugin or package_name,
            "source": request.source,
            "revision": revision if revision != "local" else checksum,
            "digest": checksum,
            "enabled": True,
            "skills": skills,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "registry.lock")):
            destination = self.root / identifier
            if not destination.exists():
                with tempfile.TemporaryDirectory(dir=self.root) as staging:
                    for name, path in included.items():
                        target = Path(staging) / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
                    Path(staging).rename(destination)
            packages = self.packages()
            if any(p["id"] == identifier for p in packages):
                return next(p for p in packages if p["id"] == identifier)
            # Updates are explicit installs. Old versions remain available for provenance.
            for package in packages:
                if package["source"] == request.source and package["name"] == record["name"]:
                    package["enabled"] = False
            write_private(self.index, {"packages": [*packages, record]})
        return record

    def enable(self, identifier: str, enabled: bool) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.root / "registry.lock")):
            packages = self.packages()
            package = next((p for p in packages if p["id"] == identifier), None)
            if package is None:
                raise AgentError("plugin_not_found", "插件不存在。", 404)
            if enabled:
                for other in packages:
                    if other["source"] == package["source"] and other["name"] == package["name"]:
                        other["enabled"] = False
            package["enabled"] = enabled
            write_private(self.index, {"packages": packages})
        return package

    def locate(self, identifier: str) -> tuple[dict, Path]:
        for package in self.packages():
            for skill in package["skills"]:
                if identifier == f"{package['id']}/{skill['key']}" and package["enabled"]:
                    return package, self.root / package["id"] / skill["directory"]
        raise AgentError("skill_not_found", "Skill 不存在或插件已停用。", 404)

    def read(self, args: SkillRead) -> dict:
        package, directory = self.locate(args.skill)
        path = directory / relative_path(args.path)
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise AgentError("skill_file_missing", "Skill 文件不存在。", 404)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise AgentError(
                "skill_file_binary", "此文件为二进制，请先 materialize 后通过文件工具读取。", 422
            ) from exc
        return {
            "skill": args.skill,
            "revision": package["revision"],
            "path": args.path,
            "content": content[args.offset : args.offset + args.length],
            "total_characters": len(content),
            "local_directory": str(directory),
            "files": [str(p.relative_to(directory)) for p in files(directory)]
            if args.path == "SKILL.md"
            else [],
        }

    def materialize(self, identifier: str, workspace: Path) -> dict:
        """Make bundled scripts usable by any native Agent without a special runtime."""
        package, source = self.locate(identifier)
        target = workspace / ".chem-skills" / identifier
        if not target.resolve().is_relative_to(workspace.resolve()):
            raise AgentError("skill_path_invalid", "Skill 目标目录越过工作空间。", 422)
        target.mkdir(parents=True, exist_ok=True)
        for path in files(source):
            destination = target / path.relative_to(source)
            if destination.is_symlink() or not destination.resolve().is_relative_to(
                target.resolve()
            ):
                raise AgentError("skill_path_invalid", "Skill 目标包含外部链接。", 422)
            if destination.exists():
                continue  # A repeated lookup must not overwrite the Agent's working edits.
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        return {"directory": str(target), "revision": package["revision"]}
