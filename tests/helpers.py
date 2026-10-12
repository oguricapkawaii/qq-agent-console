"""测试共享工具：假适配器、记录型传输、完整控制台装配。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agents.base import AgentAdapter
from app.agents.registry import AgentRegistry
from app.backends.gateway import ExecutionGateway
from app.commands.presets import PresetSpec, presets_from_config
from app.commands.registry import CommandRegistry
from app.config import Settings
from app.models import AgentReply, ChatMessage, InboundMessage, InvocationOptions
from app.routing.dispatcher import Dispatcher
from app.security.acl import AclService
from app.security.authz import Authorizer
from app.security.confirmations import InMemoryConfirmationStore
from app.security.exec_policy import ExecPolicy, policy_from_config
from app.security.rate_limit import SlidingWindowRateLimiter
from app.security.redaction import SecretRedactor
from app.storage import ContextStore
from app.tasks.service import TaskService
from app.tasks.store import TaskStore
from app.transports.base import Transport
from app.workspaces.broker import BrokerBudget
from app.workspaces.configurator import build_workspace_config
from app.workspaces.coordinator import WorkspaceCoordinator
from app.workspaces.service import WorkspaceService
from app.workflows import (
    Notifier,
    StepExecutor,
    TeamCommandHandler,
    WorkflowRunner,
    WorkflowService,
    WorkflowStore,
)

def requires_platform_sandbox():
    """exec 用例的平台前提：本机必须有可验证隔离（macOS sandbox-exec）。

    没有隔离时 exec 会被实现明确禁用，这些用例应当 skip，而不是失败或偷偷放行。
    """
    import pytest

    from app.security.sandbox import available, self_check

    return pytest.mark.skipif(not available(), reason=f"exec 平台隔离不可用：{self_check()}")


ALLOWED_USER = 100
OTHER_USER = 101
ADMIN_USER = 100
ALLOWED_GROUP = 200
OTHER_GROUP = 201
BOT_SELF = 999


class RecordingTransport(Transport):
    """记录所有外发消息的假传输。"""

    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_group_message(self, group_id: int, text: str) -> None:
        self.sends.append((group_id, text))

    def texts(self) -> list[str]:
        return [text for _, text in self.sends]


class FakeAgent(AgentAdapter):
    """可编程的假 Agent：记录收到的消息、选项与超时，按配置返回。"""

    adapter_type = "fake"

    def __init__(
        self,
        name: str,
        display_name: str | None = None,
        reply: str = "fake-reply",
        error: str | None = None,
        delay: float = 0.0,
        hook=None,
        enabled: bool = True,
        configured: bool = True,
        thinking: str = "",
        tool_log: list[str] | None = None,
        model_name: str | None = None,
        model_options: dict[str, tuple[str, ...]] | None = None,
        native_modes: frozenset[str] | None = None,
        broker_modes: frozenset[str] | None = None,
        isolation: str = "broker",
        runtime_options: dict | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name or name.upper()
        self.model_name = model_name or f"fake-{name}"
        self.enabled = enabled
        self._configured = configured
        self._reply = reply
        self._error = error
        self._delay = delay
        self._hook = hook
        self._thinking = thinking
        self._tool_log = list(tool_log or [])
        self._model_options = dict(model_options) if model_options else {self.model_name: ()}
        if native_modes is not None:
            self.native_modes = frozenset(native_modes)
        if broker_modes is not None:
            self.broker_modes = frozenset(broker_modes)
        self.isolation = isolation
        self.runtime_options = dict(runtime_options or {})
        self.calls: list[list[ChatMessage]] = []
        self.options_log: list[InvocationOptions] = []
        self.timeouts: list[float] = []

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def model_options(self) -> dict[str, tuple[str, ...]]:
        return dict(self._model_options)

    async def chat(self, messages, *, timeout: float) -> AgentReply:
        self.calls.append(list(messages))
        if self._hook is not None:
            await self._hook(self, messages)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            return AgentReply(error=self._error)
        if callable(self._reply):
            return AgentReply(
                content=self._reply(self, messages), thinking=self._thinking,
                tool_log=self._tool_log,
            )
        return AgentReply(content=self._reply, thinking=self._thinking, tool_log=self._tool_log)

    async def chat_with_options(self, messages, *, timeout: float,
                                options: InvocationOptions) -> AgentReply:
        self.validate_options(options)
        self.options_log.append(options)
        self.timeouts.append(timeout)
        return await self.chat(messages, timeout=timeout)

    def last_prompt(self) -> str:
        """最后一条 user 消息（结构化输入正文）。"""
        return self.calls[-1][-1].content if self.calls else ""


class ScriptedAgent(FakeAgent):
    """按脚本依次回放答复（用于 broker 动作循环测试）。"""

    def __init__(self, name: str, script: list[str], **kwargs) -> None:
        super().__init__(name, reply="", **kwargs)
        self._script = list(script)
        self._cursor = 0

    async def chat(self, messages, *, timeout: float) -> AgentReply:
        self.calls.append(list(messages))
        await asyncio.sleep(0)
        if self._cursor >= len(self._script):
            return AgentReply(error="harness_failed")
        text = self._script[self._cursor]
        self._cursor += 1
        return AgentReply(content=text)


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        qq_allowed_user_ids=[ALLOWED_USER],
        qq_allowed_group_ids=[ALLOWED_GROUP],
        database_path=str(tmp_path / "test.db"),
        request_timeout_seconds=2.0,
        max_context_turns=8,
        max_message_length=1500,
        max_thinking_chars=800,
        thinking_archive_dir="",  # 默认关闭归档，避免测试写入真实 runtime/state
        rate_limit_per_minute=50,
        approval_ttl_seconds=300,
        # 协作默认值：测试显式固定，避免受进程环境变量影响
        team_enabled=True,
        team_max_steps=8,
        team_max_active_per_user=2,
        team_max_active_global=4,
        team_max_parallel_steps=2,
        team_max_prompt_chars=12000,
        team_max_input_chars=60000,
        team_max_output_chars=30000,
        team_workflow_timeout_seconds=10.0,
        team_step_timeout_seconds=2.0,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def workspace_settings(tmp_path, root: Path | None = None, **overrides) -> Settings:
    """带一个授权根（成员 read/edit/exec，管理员同）的设置。"""
    root = root or tmp_path
    workspaces = overrides.pop(
        "console_workspaces",
        {
            "roots": [
                {
                    "path": str(root),
                    "modes": ["read", "edit", "exec"],
                    "admin_modes": ["read", "edit", "exec"],
                    "searchable": True,
                    "label": "test-root",
                }
            ],
            "find": {"max_depth": 3, "max_candidates": 5, "max_seconds": 2.0},
        },
    )
    return make_settings(tmp_path, console_workspaces=workspaces, **overrides)


def make_message(
    text: str,
    user_id: int = ALLOWED_USER,
    group_id: int = ALLOWED_GROUP,
    self_id: int = BOT_SELF,
) -> InboundMessage:
    return InboundMessage(group_id=group_id, user_id=user_id, self_id=self_id, text=text)


class ConsoleHarness:
    """装配一整套统一控制台组件（临时 DB + 记录型传输 + 假 Agent）。"""

    def __init__(
        self,
        settings: Settings,
        agents: dict[str, AgentAdapter],
        *,
        transport: Transport | None = None,
        authz: Authorizer | None = None,
        exec_policy: ExecPolicy | None = None,
        presets: dict[str, PresetSpec] | None = None,
        from_settings: bool = False,
    ) -> None:
        self.settings = settings
        self.agents = agents
        self.transport = transport or RecordingTransport()
        self.context_store = ContextStore(settings.database_path)
        self.task_store = TaskStore(self.context_store.connection)
        self.task_store.migrate()
        self.workflow_store = WorkflowStore(self.context_store.connection)
        self.workflow_store.migrate()
        if from_settings:
            # 走真实配置路径：验证"只加配置即可接入新后端"
            self.registry = AgentRegistry.from_settings(settings)
        else:
            self.registry = AgentRegistry(agents, exec_policy_enabled=True)
        self.backends = self.registry.backends
        self.authz = authz or Authorizer(
            settings.qq_allowed_user_ids, settings.qq_allowed_group_ids
        )
        self.authz.store = self.context_store
        self.redactor = SecretRedactor()
        self.workspace_config = build_workspace_config(settings)
        self.acl = AclService(
            authz=self.authz, grants=self.workspace_config.grants,
            protected_roots=self.workspace_config.protected,
            exec_enabled=self.workspace_config.exec_enabled,
        )
        self.confirmations = InMemoryConfirmationStore(
            default_ttl_seconds=settings.approval_ttl_seconds
        )
        self.coordinator = WorkspaceCoordinator(
            acquire_timeout=settings.workspace_lock_timeout_seconds
        )
        self.workspaces = WorkspaceService(
            acl=self.acl, store=self.task_store, confirmations=self.confirmations,
            coordinator=self.coordinator, limits=self.workspace_config.limits,
        )
        self.exec_policy = exec_policy or policy_from_config(
            (settings.console_commands or {}).get("exec")
        )
        self.budget = BrokerBudget()
        self.gateway = ExecutionGateway(
            registry=self.backends, settings=settings, redactor=self.redactor,
            exec_policy=self.exec_policy, broker_budget=self.budget,
        )
        self.rate_limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute)
        self.tasks = TaskService(
            settings=settings, registry=self.backends, gateway=self.gateway, acl=self.acl,
            workspaces=self.workspaces, coordinator=self.coordinator, store=self.task_store,
            confirmations=self.confirmations, rate_limiter=self.rate_limiter,
            redactor=self.redactor, context_store=self.context_store,
            broker_budget=self.budget, exec_policy=self.exec_policy,
            backup_root=self.workspace_config.backup_dir,
        )
        self.notifier = Notifier(
            transport=self.transport, authz=self.authz,
            redactor=self.redactor, settings=settings,
        )
        self.executor = StepExecutor(
            tasks=self.tasks, store=self.workflow_store, authz=self.authz,
            settings=settings, redactor=self.redactor, archive_dir="",
        )
        self.runner = WorkflowRunner(
            store=self.workflow_store, executor=self.executor,
            notifier=self.notifier, settings=settings,
        )
        self.team_service = WorkflowService(
            store=self.workflow_store, runner=self.runner, registry=self.registry,
            settings=settings, authz=self.authz, rate_limiter=self.rate_limiter,
            tasks=self.tasks, workspaces=self.workspaces, confirmations=self.confirmations,
        )
        self.handler = TeamCommandHandler(
            service=self.team_service, notifier=self.notifier, settings=settings
        )
        self.commands = CommandRegistry()
        self.presets = dict(presets or {})
        if not self.presets:
            self.presets = presets_from_config(
                (settings.console_commands or {}).get("presets")
            )
        self.commands.register_presets(self.presets)
        self.dispatcher = Dispatcher(
            settings=settings, registry=self.backends, store=self.context_store,
            authz=self.authz, rate_limiter=self.rate_limiter,
            confirmations=self.confirmations, transport=self.transport,
            redactor=self.redactor, tasks=self.tasks, workspaces=self.workspaces,
            acl=self.acl, commands=self.commands, presets=self.presets, team=self.handler,
        )

    async def command(
        self, text: str, *, user_id: int = ALLOWED_USER, group_id: int = ALLOWED_GROUP
    ) -> str:
        """发送一条命令并返回新增的外发文本（拼接）。"""
        before = len(self.transport.sends)
        await self.dispatcher.handle_message(make_message(text, user_id=user_id, group_id=group_id))
        return "\n".join(self.transport.texts()[before:])

    def texts_since(self, index: int) -> list[str]:
        return self.transport.texts()[index:]

    def send_count(self) -> int:
        return len(self.transport.sends)

    async def wait_team(self, team_id: str, timeout: float = 5.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            workflow = self.workflow_store.get_workflow(team_id)
            if workflow is not None and workflow.status not in ("queued", "running"):
                return workflow.status
            await asyncio.sleep(0.01)
        workflow = self.workflow_store.get_workflow(team_id)
        return workflow.status if workflow else "missing"

    def create_team(self, goal: str = "测试目标", **kwargs) -> str:
        record = self.workflow_store.create_draft(
            group_id=kwargs.get("group_id", ALLOWED_GROUP),
            owner_user_id=kwargs.get("user_id", ALLOWED_USER),
            goal=goal,
        )
        return record.team_id

    def add_step(
        self, team_id: str, step_id: str, agent: str, instruction: str = "做点什么",
        *, after: tuple[str, ...] = (), model: str | None = None,
        effort: str | None = None, effort_set: bool = False, mode: str = "text",
        backend: str | None = None, workspace: str | None = None,
    ) -> None:
        self.workflow_store.replace_step(
            team_id=team_id, step_id=step_id, agent=agent, instruction=instruction,
            depends_on=after, model=model, effort=effort, effort_set=effort_set,
            mode=mode, backend=backend, workspace=workspace,
        )

    async def aclose(self) -> None:
        await self.runner.shutdown()
        self.context_store.close()


def build_dispatcher(
    settings: Settings,
    agents: dict[str, AgentAdapter],
    **kwargs,
):
    """装配一个可直接测试的 Dispatcher（返回 dispatcher 与记录型传输）。

    需要完整组件（task_store / confirmations / acl 等）时请直接构造 ConsoleHarness。
    """
    harness = ConsoleHarness(settings, agents, **kwargs)
    return harness.dispatcher, harness.transport


class TeamHarness(ConsoleHarness):
    """兼容旧测试的别名：完整组件装配（含 team）。"""


def make_team_settings(tmp_path, **overrides) -> Settings:
    """协作测试常用设置：小并发、短超时（保留旧测试的调用点）。"""
    return make_settings(
        tmp_path,
        team_max_parallel_steps=overrides.pop("team_max_parallel_steps", 2),
        team_step_timeout_seconds=overrides.pop("team_step_timeout_seconds", 2.0),
        team_workflow_timeout_seconds=overrides.pop("team_workflow_timeout_seconds", 10.0),
        **overrides,
    )
