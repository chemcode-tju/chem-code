"""Durable backend choices and session bindings, independent of model providers."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from chemistry_agent.backends.delivery import BackendDeliveries
from chemistry_agent.backends.runtime import NativeSessions
from chemistry_agent.errors import AgentError


class BackendSettings(BaseModel):
    default_backend: str = Field(default="builtin", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    codex_executable: str = Field(default="codex", min_length=1, max_length=1024)
    codex_model: str = Field(default="", max_length=160)
    codex_sandbox: Literal["workspace-write", "danger-full-access"] = "workspace-write"
    codex_effort: str = ""
    antigravity_executable: str = Field(default="agy", min_length=1, max_length=1024)
    antigravity_model: str = Field(default="", max_length=160)
    antigravity_effort: str = ""
    reasoning_effort: str = ""
    permission_mode: Literal["user", "auto_review", "full"] | None = None


class SessionSelection(BaseModel):
    backend: Literal["builtin", "codex", "antigravity"]
    model: str = Field(default="", max_length=160)
    effort: Literal[
        "",
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "extra",
        "max",
        "ultra",
        "enabled",
        "disabled",
    ] = ""
    permission: Literal["user", "auto_review", "full"]


class AgentBackend(Protocol):
    async def run(self, messages: list[dict], progress, **kwargs) -> dict: ...


class BackendRouter:
    """Both user messages and durable event deliveries use this same selection."""

    def __init__(self, store, builtin, registry, root: Path, instructions: str) -> None:
        from chemistry_agent.backends.codex import CodexBackend

        self.store, self.root = store, root
        self.sessions = NativeSessions(root.parent)
        self.workspace_files = None
        from chemistry_agent.backends.approvals import ApprovalBroker

        self.approvals = ApprovalBroker(store)
        self.deliveries = BackendDeliveries(store)
        from chemistry_agent.backends.context import NativeContext

        self.context = NativeContext(self)
        self.adapters: dict[str, AgentBackend] = {}
        self.descriptors: dict[str, tuple[str, Callable]] = {}
        self.register("builtin", builtin, name="Chem Agent")
        self.codex = CodexBackend(self, registry, instructions)
        self.register(
            "codex",
            self.codex,
            name="Codex",
            available=lambda config: self.codex.executable(config) is not None,
        )
        from chemistry_agent.backends.antigravity import AntigravityBackend

        self.antigravity = AntigravityBackend(self, registry, instructions)
        self.register(
            "antigravity",
            self.antigravity,
            name="Antigravity",
            available=lambda config: self.antigravity.executable(config) is not None,
        )

    def register(
        self,
        identifier: str,
        adapter: AgentBackend,
        *,
        name: str | None = None,
        available: Callable | None = None,
    ) -> None:
        self.adapters[identifier] = adapter
        self.descriptors[identifier] = (name or identifier, available or (lambda config: True))

    async def initialize(self) -> None:
        async with self.store.transaction():
            await self.deliveries.initialize()
            await self.approvals.initialize()
            await self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS agent_backend_settings "
                "(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            await self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS agent_bindings "
                "(session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, "
                "backend TEXT NOT NULL, external_thread_id TEXT, external_turn_id TEXT, "
                "turn_state TEXT, config TEXT NOT NULL)"
            )
            await self.store.db.execute(
                "UPDATE agent_bindings SET turn_state='uncertain' "
                "WHERE turn_state IN ('starting','running')"
            )
            await self.store.db.execute(
                "UPDATE backend_deliveries SET state='uncertain' WHERE state='started'"
            )
            await self.context.initialize()
            # Existing research sessions must not change consumer when the default changes.
            await self.store.db.execute(
                "INSERT OR IGNORE INTO agent_bindings(session_id,backend,config) "
                "SELECT id,'builtin','{}' FROM sessions"
            )

    async def settings(self) -> BackendSettings:
        async with self.store.lock:
            row = await self.store._one("SELECT payload FROM agent_backend_settings WHERE id=1", ())
        return BackendSettings.model_validate_json(row["payload"]) if row else BackendSettings()

    async def save_settings(self, config: BackendSettings) -> dict:
        if config.default_backend not in self.adapters:
            raise AgentError("agent_backend_unavailable", "此 Agent 适配器尚未安装。", 422)
        async with self.store.transaction():
            await self.store.db.execute(
                "INSERT INTO agent_backend_settings VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (config.model_dump_json(),),
            )
        return config.model_dump()

    async def binding(self, session: str | None, *, create: bool = False) -> dict:
        async with self.store.lock:
            row = await self.store._one(
                "SELECT * FROM agent_bindings WHERE session_id=?", (session,)
            )
        if row:
            return dict(row)
        config = await self.settings()
        binding = {
            "backend": config.default_backend,
            "config": config.model_dump_json(),
            "external_thread_id": None,
            "external_turn_id": None,
            "turn_state": None,
        }
        if create:
            async with self.store.transaction():
                await self.store.db.execute(
                    "INSERT OR IGNORE INTO agent_bindings(session_id,backend,config) VALUES(?,?,?)",
                    (session, binding["backend"], binding["config"]),
                )
            return await self.binding(session)
        return binding

    async def update_thread(self, session: str, **values) -> None:
        allowed = {"external_thread_id", "external_turn_id", "turn_state"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid binding update")
        async with self.store.transaction():
            await self.store.db.execute(
                "UPDATE agent_bindings SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE session_id=?",
                (*values.values(), session),
            )

    async def selection(self, session: str | None, provider) -> dict:
        binding = await self.binding(session)
        config = BackendSettings.model_validate_json(binding["config"])
        owner, model_config = provider.configuration()
        builtin = binding["backend"] == "builtin"
        model = getattr(config, f"{binding['backend']}_model", "")
        if builtin:
            model = (
                await self.store.get_session_model(
                    session, owner, model_config.provider, model_config.base_url
                )
                if session
                else None
            ) or model_config.model
        return {
            "backend": binding["backend"],
            "model": model,
            "effort": config.reasoning_effort
            if builtin
            else getattr(config, f"{binding['backend']}_effort", ""),
            "permission": config.permission_mode
            or (
                "full"
                if builtin
                or (binding["backend"] == "codex" and config.codex_sandbox == "danger-full-access")
                else "user"
            ),
        }

    async def select(self, session: str, choice: SessionSelection, provider) -> dict:
        """Change idle sessions atomically; a new backend receives the platform transcript."""
        if choice.backend == "builtin" and choice.permission == "auto_review":
            raise AgentError("permission_unsupported", "Chem Agent 暂不支持自动审批。", 422)
        owner, model_config = provider.configuration()
        if choice.backend == "codex" and choice.effort in {"enabled", "disabled"}:
            raise AgentError("effort_unsupported", "Codex 不支持此推理选项。", 422)
        if choice.backend == "builtin" and choice.effort:
            if model_config.provider == "deepseek" and choice.effort not in {"enabled", "disabled"}:
                raise AgentError("effort_unsupported", "DeepSeek 支持开启或关闭思考。", 422)
            if model_config.provider == "openai-compatible" or (
                model_config.provider == "chatgpt" and choice.effort in {"enabled", "disabled"}
            ):
                raise AgentError("effort_unsupported", "此模型服务未声明该推理选项。", 422)
        binding = await self.binding(session, create=True)
        changed = choice.backend != binding["backend"]
        config = (
            await self.settings()
            if changed
            else BackendSettings.model_validate_json(binding["config"])
        )
        config.permission_mode = choice.permission
        if choice.backend == "codex":
            config.codex_model, config.codex_effort = choice.model, choice.effort
            config.codex_sandbox = (
                "danger-full-access" if choice.permission == "full" else "workspace-write"
            )
        elif choice.backend == "antigravity":
            config.antigravity_model, config.antigravity_effort = choice.model, choice.effort
        else:
            config.reasoning_effort = choice.effort
        async with self.store.transaction():
            row = await self.store._one(
                "SELECT active_run_id FROM sessions WHERE id=? AND owner_id IS ?", (session, owner)
            )
            queued = await self.store._one(
                "SELECT id FROM chat_requests WHERE session_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (session,),
            )
            if row is None:
                raise AgentError("session_not_found", "会话不存在。", 404)
            if row["active_run_id"] or queued:
                raise AgentError(
                    "agent_selection_busy", "本轮结束后可以切换 Agent 与执行配置。", 409
                )
            await self.store.db.execute(
                "UPDATE agent_bindings SET backend=?,config=?,external_thread_id=?,"
                "external_turn_id=?,turn_state=? WHERE session_id=?",
                (
                    choice.backend,
                    config.model_dump_json(),
                    None if changed else binding["external_thread_id"],
                    None if changed else binding["external_turn_id"],
                    None if changed else binding["turn_state"],
                    session,
                ),
            )
            previous = BackendSettings.model_validate_json(binding["config"])
            if not changed and choice.model != getattr(previous, f"{choice.backend}_model", ""):
                data = await self.context._read(
                    session, choice.backend, binding["external_thread_id"]
                )
                data.update(used_tokens=None, capacity_tokens=None, updated_at=None)
                if binding["external_thread_id"]:
                    await self.context._save(
                        session, choice.backend, binding["external_thread_id"], data
                    )
            if choice.backend == "builtin":
                await self.store.db.execute(
                    "INSERT INTO session_models VALUES (?,?,?,?) ON CONFLICT(session_id) "
                    "DO UPDATE SET provider=excluded.provider,base_url=excluded.base_url,"
                    "model=excluded.model",
                    (session, model_config.provider, model_config.base_url, choice.model or None),
                )
        return await self.selection(session, provider)

    async def run(self, messages: list[dict], progress, **kwargs) -> dict:
        binding = await self.binding(kwargs["session_id"], create=True)
        adapter = self.adapters.get(binding["backend"])
        if adapter is None:
            raise AgentError("agent_backend_unavailable", "此会话的 Agent 后端未安装。", 503)
        from chemistry_agent.backends.approvals import current_tool_approval
        from chemistry_agent.model_runtime import current_effort

        config = BackendSettings.model_validate_json(binding["config"])
        mode = config.permission_mode or (
            "full"
            if binding["backend"] == "builtin"
            or (binding["backend"] == "codex" and config.codex_sandbox == "danger-full-access")
            else "user"
        )
        approval_token = current_tool_approval.set(self.approvals if mode == "user" else None)
        effort_token = current_effort.set(config.reasoning_effort)
        try:
            if binding["backend"] != "builtin":
                kwargs["binding"] = binding
                return await self.deliveries.execute(adapter, messages, progress, **kwargs)
            return await adapter.run(messages, progress, **kwargs)
        finally:
            current_tool_approval.reset(approval_token)
            current_effort.reset(effort_token)
            if self.workspace_files:
                import logging

                try:
                    await self.workspace_files.publish(
                        kwargs["session_id"], [], kwargs.get("run_id")
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Workspace output discovery delayed")

    async def ready(self, session: str | None = None) -> bool:
        binding = await self.binding(session)
        descriptor = self.descriptors.get(binding["backend"])
        if not descriptor:
            return False
        config = BackendSettings.model_validate_json(binding["config"])
        return bool(descriptor[1](config))

    async def public(self) -> dict:
        config = await self.settings()
        return {
            "settings": config.model_dump(),
            "backends": [
                {"id": identifier, "name": name, "available": bool(available(config))}
                for identifier, (name, available) in self.descriptors.items()
            ],
        }
