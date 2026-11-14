"""Materialize explicit file dependencies without passing binary data through the model."""

import json
import shutil
from pathlib import Path
from uuid import UUID

from chemistry_agent.attachments import session_file
from chemistry_agent.automation.job_models import FileReference, artifact_path
from chemistry_agent.errors import AgentError


def local_file(directory: Path, name: str) -> Path:
    path = directory / artifact_path(name, reserved=False)
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise AgentError("file_outside_workspace", "文件链接越过工作目录。", 422)
    return path


async def stage_dependencies(
    plans,
    session: str,
    plan: dict,
    node: dict,
    directory: Path,
    remote_destination: dict | None = None,
) -> dict:
    manifest = {}
    for raw in node.get("files", []):
        ref = FileReference.model_validate(raw)
        destination = local_file(directory, ref.target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if ref.artifact_id:
            plans.chat.load(session, "artifacts", str(ref.artifact_id))
            source = plans.chat.directory(session, "artifact_blobs") / str(ref.artifact_id)
            shutil.copyfile(source, destination)
        elif ref.file_id:
            store = plans.chat.attachment_store
            if store is None:
                raise AgentError("file_not_found", "附件存储不可用。", 404)
            _, source = await session_file(store, session, ref.file_id)
            if isinstance(source, Path):
                shutil.copyfile(source, destination)
            else:
                destination.write_bytes(source)
        else:
            state = plan["runtime"].get(ref.node, {}) if ref.node else {}
            if ref.node and state.get("status") != "completed":
                raise AgentError("dependency_pending", "上游文件尚未就绪。", 409)
            identifier = str(ref.job_id or state.get("generic_job_id") or state.get("job_id") or "")
            generic_path = (
                plans.chat.directory(session, "slurm_jobs") / f"{identifier}.json"
                if identifier
                else None
            )
            if generic_path and generic_path.exists():
                record = plans.slurm.load(session, identifier)
                if remote_destination and record["host"] == remote_destination["host"]:
                    copied = await plans.slurm.remote(
                        remote_destination,
                        "copy",
                        source_root=record["remote_directory"],
                        source=ref.path,
                        path=ref.target,
                    )
                    manifest[ref.target] = {
                        **ref.model_dump(mode="json"),
                        "file": ref.target,
                        "transfer": "remote_copy",
                        **copied,
                    }
                    continue
                retrieved = record.get("retrieved_directory")
                saved = local_file(Path(retrieved), ref.path) if retrieved else None
                if ref.source == "retrieved" and saved and saved.is_file():
                    shutil.copyfile(saved, destination)
                else:
                    await plans.slurm.download(record, ref.path, destination)
            elif identifier:
                await plans.chat.require_session_job(session, identifier)
                staging = directory / "_dependency_cache" / str(UUID(identifier))
                result = await plans.chat.service.call(
                    "stage_files",
                    {
                        "job_id": identifier,
                        "files": [ref.path],
                        "source": ref.source,
                        "directory": str(staging),
                    },
                )
                shutil.copyfile(result["files"][ref.path], destination)
            elif ref.node:
                upstream = plans.chat.directory(session, "plan_runs") / plan["id"] / ref.node
                source = local_file(upstream, ref.path)
                if not source.is_file():
                    raise AgentError("dependency_file_missing", f"上游缺少文件 {ref.path}。", 422)
                shutil.copyfile(source, destination)
            else:
                raise AgentError("dependency_file_missing", "找不到引用的作业文件。", 404)
        manifest[ref.target] = {
            **ref.model_dump(mode="json"),
            "file": ref.target,
            "size": destination.stat().st_size,
        }
    (directory / "file_inputs.json").write_text(json.dumps(manifest, ensure_ascii=False))
    return manifest
