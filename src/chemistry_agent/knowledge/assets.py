"""Promote selected job or workspace files into the existing research memory."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from chemistry_agent.errors import AgentError
from chemistry_agent.hosting.registry import Hosting
from chemistry_agent.knowledge.asset_io import FILE_LIMIT, TOTAL_LIMIT, asset_path, digest, folder
from chemistry_agent.knowledge.asset_io import freeze as save_blob
from chemistry_agent.knowledge.asset_vasp import HEAD_BYTES, TAIL_BYTES, VASP_FILES, vasp_facts
from chemistry_agent.memory.models import MemoryDocument, MemorySearch
from chemistry_agent.memory.scope import MemoryScope
from chemistry_agent.memory.service import ResearchMemory

BUNDLE = "chem-asset.json"
OUTCAR_HEADER = "OUTCAR.head.txt"
OUTCAR_TAIL = "OUTCAR.tail.txt"
SOURCE_PREFIX = "asset:"
Kind = Literal["vasp", "script", "files"]
FileName = Annotated[str, Field(min_length=1, max_length=512)]
Host = Annotated[str, Field(min_length=1, max_length=96)]
Directory = Annotated[str, Field(min_length=1, max_length=2048)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Location(Contract):
    host: Host = "local"
    directory: Directory

    @field_validator("directory")
    @classmethod
    def valid_directory(cls, value: str) -> str:
        return asset_path(value, relative=False)


class ImportAsset(Location):
    kind: Literal["vasp", "script", "files", "bundle"] = "vasp"
    files: list[FileName] = Field(default_factory=list, max_length=24)
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    systems: list[str] = Field(default_factory=list, max_length=20)
    environment: str = Field(default="", max_length=1000)

    @field_validator("files")
    @classmethod
    def valid_files(cls, values: list[str]) -> list[str]:
        files = sorted({asset_path(value, relative=True) for value in values})
        if any(name in {BUNDLE, OUTCAR_HEADER, OUTCAR_TAIL} for name in files):
            raise ValueError("保留文件名由平台生成")
        return files

    @model_validator(mode="after")
    def check_files(self) -> "ImportAsset":
        if self.kind in {"files", "script"} and not self.files:
            raise ValueError("请指定确切的文件列表")
        if self.kind == "bundle" and self.files:
            raise ValueError("资产包由自身清单指定文件")
        return self


class SearchAssets(Contract):
    query: str = Field(default="", max_length=1000)
    scope: Literal["project", "personal"] = "project"
    kind: Kind | None = None
    formula: str | None = Field(default=None, max_length=100)
    functional: str | None = Field(default=None, max_length=50)
    limit: int = Field(default=8, ge=1, le=20)


class AssetVersion(Contract):
    asset_id: UUID
    version: int = Field(ge=1)


class CheckoutAsset(Location, AssetVersion):
    pass


class AssetFile(Contract):
    name: FileName
    size: int = Field(ge=0, le=FILE_LIMIT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        name = asset_path(value, relative=True)
        if name == BUNDLE:
            raise ValueError("资产包清单不能索引自身")
        return name


class AssetManifest(Contract):
    format: Literal["chem-agent-asset/1"] = "chem-agent-asset/1"
    kind: Kind
    source: Location
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000)
    systems: list[str] = Field(max_length=20)
    environment: str = Field(max_length=1000)
    files: list[AssetFile] = Field(min_length=1, max_length=32)
    facts: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_manifest(self) -> "AssetManifest":
        names = {file.name for file in self.files}
        if len(names) != len(self.files) or sum(file.size for file in self.files) > TOTAL_LIMIT:
            raise ValueError("资产包文件冲突或过大")
        if any(str(parent) in names for name in names for parent in Path(name).parents):
            raise ValueError("资产包路径同时被用作文件与目录")
        if self.facts.get("scientifically_validated", False) is not False:
            raise ValueError("历史资产导入不接受未经平台验收的科学验证声明")
        return self


def encode(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


class Assets:
    def __init__(self, memory: ResearchMemory, root: Path, hosting: Hosting) -> None:
        self.memory, self.root, self.hosting = memory, root / "research-assets" / "objects", hosting

    async def capture(self, args: ImportAsset) -> tuple[AssetManifest, dict[str, bytes]]:
        async with folder(self.hosting, args.host, args.directory) as source:
            if args.kind == "bundle":
                return await read_bundle(source)
            files = {}
            candidates = list(args.files)
            if args.kind == "vasp":
                candidates = list(dict.fromkeys((*VASP_FILES, *candidates)))
            total = 0
            for name in candidates:
                item = await source.read(name, FILE_LIMIT)
                if item is None:
                    if name in args.files:
                        raise AgentError("asset_file_missing", f"目录中找不到 {name}。", 404)
                    continue
                content, size = item
                total += size
                if total > TOTAL_LIMIT:
                    raise AgentError("asset_size", "资产超过 32 MiB，请缩小文件范围。", 413)
                if content:
                    files[name] = content
            facts = {}
            if args.kind == "vasp":
                head, size = await source.read("OUTCAR", HEAD_BYTES, tail=False) or (b"", 0)
                tail, tail_size = await source.read("OUTCAR", TAIL_BYTES, tail=True) or (b"", 0)
                if size != tail_size:
                    raise AgentError("asset_source_changed", "OUTCAR 仍在变化，请稍后重试。", 409)
                if tail:
                    files.update({OUTCAR_HEADER: head, OUTCAR_TAIL: tail})
                if not set(files).intersection({"POSCAR", "CONTCAR", OUTCAR_TAIL}):
                    raise AgentError("asset_vasp_missing", "目录缺少 VASP 结构或 OUTCAR。", 422)
                facts = vasp_facts(files, head, tail, size)
            if not files:
                raise AgentError("asset_file_missing", "目录中没有可保存的非空文件。", 422)
            manifest = AssetManifest(
                kind=args.kind,
                source=Location(host=args.host, directory=str(source.root)),
                title=args.title
                or (Path(args.files[0]).name if len(args.files) == 1 else source.root.name)
                or args.kind,
                description=args.description,
                systems=args.systems,
                environment=args.environment,
                files=[
                    AssetFile(name=name, size=len(content), sha256=digest(content))
                    for name, content in sorted(files.items())
                ],
                facts=facts,
            )
        return manifest, files

    async def publish(self, scope: MemoryScope, args: ImportAsset) -> dict:
        manifest, files = await self.capture(args)
        for content in files.values():
            await asyncio.to_thread(save_blob, self.root, content)
        payload = manifest.model_dump(mode="json")
        checksum = digest(encode(payload))
        identity = {
            "source": payload["source"],
            "kind": manifest.kind,
            "selection": sorted(files) if manifest.kind != "vasp" else [],
        }
        document = MemoryDocument(
            title=manifest.title,
            kind="calculation" if manifest.kind == "vasp" else "document",
            content=manifest.description or "已保存固定版本，复用前请核对来源与适用条件。",
            systems=manifest.systems,
            tasks=[manifest.kind],
        )
        record = await self.memory.save(
            MemoryScope(scope.owner_id, scope.project_id),
            document,
            source_key=SOURCE_PREFIX + digest(encode(identity)),
            evidence={
                "level": "captured",
                "scientifically_validated": False,
                "manifest": payload,
                "revision_sha256": checksum,
            },
        )
        if record.get("forgotten"):
            raise AgentError("asset_withdrawn", "该资产已撤回。", 409)
        return record | {"manifest": payload, "revision_sha256": checksum}

    async def search(self, scope: MemoryScope, args: SearchAssets) -> dict:
        return await self.memory.search(
            scope,
            MemorySearch(query=args.query, scope=args.scope, limit=args.limit),
            asset_filters=args.model_dump(include={"kind", "formula", "functional"}),
        )

    async def inspect(self, scope: MemoryScope, args: AssetVersion) -> dict:
        identifier = str(args.asset_id)
        current = await self.memory.read(scope, identifier)
        if not current["source_key"].startswith(SOURCE_PREFIX):
            raise AgentError("asset_not_found", "资产不存在或无法访问。", 404)
        item = await self.memory.version(scope, identifier, args.version)
        evidence = item["evidence"]
        return {"id": identifier, "version": args.version} | evidence

    async def checkout(self, scope: MemoryScope, args: CheckoutAsset) -> dict:
        item = await self.inspect(scope, args)
        manifest = AssetManifest.model_validate(item["manifest"])
        checksum = item["revision_sha256"]
        if digest(encode(item["manifest"])) != checksum:
            raise AgentError("asset_integrity", "资产清单的校验和不匹配。", 409)
        files = {}
        for entry in manifest.files:
            try:
                content = await asyncio.to_thread((self.root / entry.sha256).read_bytes)
            except FileNotFoundError as exc:
                raise AgentError("asset_blob_missing", "资产快照文件缺失。", 404) from exc
            if entry.size != len(content) or entry.sha256 != digest(content):
                raise AgentError("asset_integrity", f"{entry.name} 的校验和不匹配。", 409)
            files[entry.name] = content
        files[BUNDLE] = encode({"manifest": item["manifest"], "revision_sha256": checksum})
        directory_name = f"asset-{args.asset_id.hex[:8]}-v{args.version}-{uuid4().hex[:12]}"
        async with folder(self.hosting, args.host, args.directory) as target:
            result = await target.write(directory_name, files)
        return {
            "id": item["id"],
            "version": args.version,
            "revision_sha256": checksum,
            "host": args.host,
            "directory": result,
            "files": list(files),
        }


async def read_bundle(source) -> tuple[AssetManifest, dict[str, bytes]]:
    record = await source.read(BUNDLE, FILE_LIMIT)
    if record is None:
        raise AgentError("asset_manifest_missing", "目录中缺少 chem-asset.json。", 404)
    try:
        body = json.loads(record[0])
        supplied = body["manifest"]
        manifest = AssetManifest.model_validate(supplied)
        if digest(encode(supplied)) != body["revision_sha256"]:
            raise ValueError("manifest mismatch")
    except (KeyError, TypeError, ValidationError, ValueError) as exc:
        raise AgentError("asset_manifest", "资产包清单无效或校验失败。", 422) from exc
    files = {}
    for entry in manifest.files:
        item = await source.read(entry.name, entry.size)
        if item is None or item[1] != entry.size or digest(item[0]) != entry.sha256:
            raise AgentError("asset_integrity", f"{entry.name} 与资产包清单不符。", 409)
        files[entry.name] = item[0]
    return manifest, files


ASSET_TOOLS = {
    "asset_import": (
        ImportAsset,
        "保存指定本机或 SSH 目录的历史 VASP、脚本、文件或资产包。",
        "publish",
    ),
    "asset_search": (SearchAssets, "按关键词、类型、VASP 体系与泛函查找已保存的资产。", "search"),
    "asset_inspect": (AssetVersion, "读取固定资产版本的计算证据和文件清单。", "inspect"),
    "asset_checkout": (
        CheckoutAsset,
        "校验固定版本并复制到目标目录；返回可移交的资产包。",
        "checkout",
    ),
}
