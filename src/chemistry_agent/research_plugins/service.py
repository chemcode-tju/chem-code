"""Headless shared research service, reusing the platform's plan schema and writer."""

import asyncio
import time
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from chemistry_agent.automation.chat import CalculationChat
from chemistry_agent.automation.models import StrictModel
from chemistry_agent.automation.plan_models import PlanDraft
from chemistry_agent.automation.service import AutomationService
from chemistry_agent.config import Settings
from chemistry_agent.platform.contracts import OperationContext
from chemistry_agent.research_plugins.folders import checked_directory, list_folder
from chemistry_agent.research_plugins.ledger import MonitorLedger, stable_id
from chemistry_agent.research_plugins.models import (
    FolderLink,
    FolderRead,
    JobRegister,
    Scope,
    WaitControl,
    WaitList,
    WaitRead,
    WaitRegister,
)


class WorkflowStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["task"] = "task"
    depends_on: list[str] = Field(default_factory=list)
    purpose: str = Field(default="", max_length=2000)


class WorkflowDraft(StrictModel):
    plan_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(default="研究流程", min_length=1, max_length=2000)
    nodes: list[WorkflowStep] = Field(min_length=1, max_length=500)


class PlanSave(Scope):
    draft: WorkflowDraft


class PlanRead(Scope):
    plan_id: UUID | None = None


class WorkspaceOpen(Scope):
    title: str = Field(default="研究工作空间", min_length=1, max_length=200)
    project_path: str | None = Field(default=None, max_length=4096)


class ResearchService:
    def __init__(
        self,
        home: Path,
        ssh_hosts: tuple[str, ...] = (),
        project_roots: tuple[Path, ...] = (),
        local_host: str = "",
        codex_wakeup: bool = False,
    ) -> None:
        self.home, self.ssh_hosts = home, ssh_hosts
        self.local_host = local_host
        self.codex_wakeup = codex_wakeup
        self.project_roots = tuple(path.resolve() for path in project_roots)
        self.ledger = MonitorLedger(home / "monitor.sqlite3")
        # Explicit isolation: never load the application's .env, agents, or production paths.
        settings = Settings(
            _env_file=None,
            platform_path=home / "platform",
            ssh_hosts=",".join(ssh_hosts),
            research_memory_enabled=False,
        )
        self.chat = CalculationChat(AutomationService(settings))
        self.operations = {
            "workspace_open": (WorkspaceOpen, self.workspace_open),
            "monitor_register_job": (JobRegister, self.register_job),
            "monitor_list_jobs": (Scope, self.list_jobs),
            "monitor_register_wait": (WaitRegister, self.register_wait),
            "monitor_list_waits": (WaitList, self.list_waits),
            "monitor_read_wait": (WaitRead, self.read_wait),
            "monitor_control_wait": (WaitControl, self.control_wait),
            "research_save_plan": (PlanSave, self.save_plan),
            "research_read_plan": (PlanRead, self.read_plan),
            "research_link_folder": (FolderLink, self.link_folder),
            "research_read_folder": (FolderRead, self.read_folder),
        }

    def catalog(self, surface: str) -> list[dict]:
        descriptions = {
            "workspace_open": "Open a shared workspace. Use one stable UUID across both plugins.",
            "monitor_register_job": (
                "Register an existing Slurm job or a simulated job. Never submits a "
                "calculation. Reuse key only for the identical request."
            ),
            "monitor_list_jobs": (
                "Read observed jobs, scheduler states and observation errors. Scheduler "
                "completion does not imply scientific convergence."
            ),
            "monitor_register_wait": (
                "Persist a one-shot all/any group tied to the original chat UUID. "
                "When automatic wakeup is configured, it defaults to a read-only summary. "
                "Register after submitting tracked jobs unless the user opted out. "
                "Use pull for manual-only monitoring."
            ),
            "monitor_list_waits": "List job groups and their wait and Codex delivery status.",
            "monitor_read_wait": (
                "Read a durable event, optionally waiting up to 30 seconds. Reading does "
                "not acknowledge it. Retry the same wait after reconnecting."
            ),
            "monitor_control_wait": (
                "Acknowledge an event after processing it, or cancel only its wait. Does "
                "not cancel the job."
            ),
            "research_save_plan": (
                "Create or revise a draft with the existing platform DAG validation and "
                "revision guard. Does not execute it."
            ),
            "research_read_plan": "Read the workflow, associated jobs and step directory links.",
            "research_link_folder": "Associate a step with an existing project directory.",
            "research_read_folder": (
                "List all files and directories, including hidden entries, archive and symlinks, "
                "100 per page. Empty path opens the step directory; other paths are "
                "project-relative. Link targets must stay inside the project. "
                "Does not read file contents."
            ),
        }
        names = [
            name
            for name in self.operations
            if name == "workspace_open"
            or name.startswith("monitor_" if surface == "monitor" else "research_")
        ]
        readonly = {
            "monitor_list_jobs",
            "monitor_list_waits",
            "monitor_read_wait",
            "research_read_plan",
            "research_read_folder",
        }
        return [
            {
                "name": name,
                "description": descriptions[name],
                "inputSchema": self.operations[name][0].model_json_schema(),
                "annotations": {
                    "readOnlyHint": name in readonly,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            }
            for name in names
        ]

    async def execute(self, name: str, arguments: dict) -> dict:
        if name not in self.operations:
            raise ValueError("未知的研究平台工具")
        model, handler = self.operations[name]
        args = model.model_validate(arguments)
        if name != "workspace_open":
            self.chat.load(str(args.workspace_id), "workspaces", str(args.workspace_id))
        return await handler(args)

    async def workspace_open(self, args: WorkspaceOpen) -> dict:
        identifier = str(args.workspace_id)
        from chemistry_agent.errors import AgentError

        project = (
            str(checked_directory(Path(args.project_path), self.project_roots))
            if args.project_path
            else None
        )
        try:
            previous = self.chat.load(identifier, "workspaces", identifier)
            if project:
                if previous.get("project_path") and previous["project_path"] != project:
                    raise ValueError("工作空间已关联其他项目，请为新项目使用新的 workspace_id")
                previous["project_path"] = project
                self.chat.save(identifier, "workspaces", identifier, previous)
            return previous
        except AgentError as error:
            if error.status_code != 404:
                raise
        record = {"id": identifier, "title": args.title, "project_path": project}
        self.chat.save(identifier, "workspaces", identifier, record)
        return record

    def binding(self, workspace: str, plan_id: str, node_id: str) -> dict:
        plan = self.chat.load(workspace, "plans", plan_id)
        if node_id not in plan["runtime"]:
            raise ValueError("当前方案中不存在此步骤")
        return plan

    async def register_job(self, args: JobRegister) -> dict:
        if (
            args.backend == "slurm"
            and args.host not in self.ssh_hosts
            and args.host != self.local_host
        ):
            raise ValueError("请先在服务配置的 local_host 或 ssh_hosts 中启用此主机")
        if args.plan_id:
            self.binding(str(args.workspace_id), str(args.plan_id), args.node_id)
        return self.ledger.register(args)

    async def list_jobs(self, args: Scope) -> dict:
        return {"jobs": self.ledger.jobs(str(args.workspace_id))}

    async def register_wait(self, args: WaitRegister) -> dict:
        delivery = args.delivery
        if delivery == "default":
            delivery = "codex" if self.codex_wakeup else "pull"
        if not self.codex_wakeup and (delivery == "codex" or args.summary):
            raise ValueError("此服务未配置 Codex 自动汇总")
        workspace = str(args.workspace_id)
        project = self.chat.load(workspace, "workspaces", workspace).get("project_path")
        return self.ledger.register_wait(args.model_copy(update={"delivery": delivery}), project)

    async def list_waits(self, args: WaitList) -> dict:
        return self.ledger.list_waits(str(args.workspace_id), args.offset)

    async def read_wait(self, args: WaitRead) -> dict:
        deadline = time.monotonic() + args.timeout_seconds
        while True:
            result = self.ledger.read_wait(str(args.workspace_id), str(args.wait_id))
            if result["status"] != "waiting" or time.monotonic() >= deadline:
                return result
            await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))

    async def control_wait(self, args: WaitControl) -> dict:
        return self.ledger.control_wait(args)

    async def save_plan(self, args: PlanSave) -> dict:
        return await self.chat.plans.draft(
            PlanDraft.model_validate(args.draft.model_dump()),
            OperationContext(str(args.workspace_id), str(uuid4())),
        )

    async def read_plan(self, args: PlanRead) -> dict:
        workspace = str(args.workspace_id)
        plans = (
            [self.chat.plans.public(self.chat.load(workspace, "plans", str(args.plan_id)))]
            if args.plan_id
            else self.chat.plans.records(workspace)
        )
        return {
            "plans": plans,
            "jobs": self.ledger.jobs(workspace),
            "folders": self.chat.list_records(workspace, "plugin_folders", limit=10000),
        }

    def project_root(self, workspace: str) -> Path:
        record = self.chat.load(workspace, "workspaces", workspace)
        if not record.get("project_path"):
            raise ValueError("尚未关联项目目录，请在对话中为工作空间指定 project_path")
        return checked_directory(Path(record["project_path"]), self.project_roots)

    async def link_folder(self, args: FolderLink) -> dict:
        workspace = str(args.workspace_id)
        self.binding(workspace, str(args.plan_id), args.node_id)
        root = self.project_root(workspace)
        path = checked_directory(root / args.path, (root,))
        identifier = stable_id(f"folder:{workspace}:{args.plan_id}:{args.node_id}")
        record = args.model_dump(mode="json") | {"id": identifier, "path": str(path)}
        self.chat.save(workspace, "plugin_folders", identifier, record)
        return record

    async def read_folder(self, args: FolderRead) -> dict:
        workspace = str(args.workspace_id)
        plan = self.binding(workspace, str(args.plan_id), args.node_id)
        root = self.project_root(workspace)
        links = self.chat.list_records(workspace, "plugin_folders", limit=10000)
        link = next(
            (
                row
                for row in links
                if row["plan_id"] == str(args.plan_id) and row["node_id"] == args.node_id
            ),
            None,
        )
        folder = checked_directory(Path(link["path"]), (root,)) if link else root
        node = next(node for node in plan["nodes"] if node["id"] == args.node_id)
        return list_folder(root, args.path or str(folder.relative_to(root)), args.offset) | {
            "node_title": node["title"],
            "project_path": str(root),
            "linked": link is not None,
        }
