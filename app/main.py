"""应用入口：装配各组件，提供健康检查、状态接口与统一 /api/v1。

启动方式：
    python -m app.main
    # 或
    uvicorn app.main:app --host 127.0.0.1 --port 8080

装配顺序即依赖顺序：
配置 → 存储/迁移 → 后端注册表 → ACL/目录服务 → 执行网关 → TaskService →
team 协作 → 命令注册表 → 调度器 → HTTP API。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

from app.agents.registry import AgentRegistry
from app.api.v1 import build_identity_table, create_router
from app.backends.gateway import ExecutionGateway
from app.backends.registry import BackendRegistry
from app.commands.presets import presets_from_config
from app.commands.registry import CommandRegistry
from app.config import Settings, load_settings
from app.logging_setup import setup_logging
from app.routing.dispatcher import Dispatcher
from app.security.acl import AclService
from app.security.authz import Authorizer
from app.security.confirmations import InMemoryConfirmationStore
from app.security.exec_policy import policy_from_config
from app.security.rate_limit import SlidingWindowRateLimiter
from app.security.redaction import SecretRedactor
from app.storage import ContextStore
from app.tasks.service import TaskService
from app.tasks.store import TaskStore
from app.transports import build_transport
from app.transports.onebot_http import OneBotHTTPTransport
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    redactor = SecretRedactor()
    for secret in (
        settings.openai_api_key,
        settings.deepseek_api_key,
        settings.glm_api_key,
        settings.onebot_access_token,
        settings.console_api_token_env and __import__("os").environ.get(
            settings.console_api_token_env, ""
        ),
    ):
        redactor.register(secret)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level, redactor)
        logger = logging.getLogger("qqbot.main")

        # 1) 存储与迁移（可重复执行；不删除历史数据）
        store = ContextStore(settings.database_path)
        task_store = TaskStore(store.connection)
        task_store.migrate()
        workflow_store = WorkflowStore(store.connection)
        workflow_store.migrate()
        interrupted_teams = workflow_store.mark_interrupted_on_startup()
        interrupted_tasks = task_store.mark_interrupted_on_startup()

        # 2) 后端注册表（三层分离；能力如实描述）
        registry_view = AgentRegistry.from_settings(settings)
        backends: BackendRegistry = registry_view.backends

        # 3) 目录配置 / ACL / 全入口目录锁 / 确认服务
        workspace_config = build_workspace_config(settings)
        authz = Authorizer(settings.qq_allowed_user_ids, settings.qq_allowed_group_ids)
        acl = AclService(
            authz=authz,
            grants=workspace_config.grants,
            protected_roots=workspace_config.protected,
            exec_enabled=workspace_config.exec_enabled,
            edit_enabled=workspace_config.edit_enabled,
        )
        confirmations = InMemoryConfirmationStore(
            default_ttl_seconds=settings.approval_ttl_seconds
        )
        coordinator = WorkspaceCoordinator(
            acquire_timeout=workspace_config.lock_timeout,
            max_workers=workspace_config.max_workers,
        )
        workspaces = WorkspaceService(
            acl=acl, store=task_store, confirmations=confirmations,
            coordinator=coordinator, limits=workspace_config.limits,
            excludes=workspace_config.excludes,
        )

        # 4) 执行网关与 TaskService（单任务/广播/team 步骤唯一入口）
        commands_config = settings.console_commands or {}
        exec_policy = policy_from_config(
            (commands_config.get("exec") if isinstance(commands_config, dict) else None)
        )
        budget_cfg = (
            commands_config.get("broker") if isinstance(commands_config, dict) else None
        ) or {}
        budget = BrokerBudget(
            max_actions=int(budget_cfg.get("max_actions") or 40),
            max_rounds=int(budget_cfg.get("max_rounds") or 8),
            max_total_read_bytes=int(budget_cfg.get("max_total_read_bytes") or 256 * 1024),
            max_file_bytes=int(budget_cfg.get("max_file_bytes") or 128 * 1024),
        )
        gateway = ExecutionGateway(
            registry=backends, settings=settings, redactor=redactor,
            exec_policy=exec_policy, broker_budget=budget,
        )
        rate_limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute)
        tasks = TaskService(
            settings=settings, registry=backends, gateway=gateway, acl=acl,
            workspaces=workspaces, coordinator=coordinator, store=task_store,
            confirmations=confirmations, rate_limiter=rate_limiter, redactor=redactor,
            context_store=store, broker_budget=budget, exec_policy=exec_policy,
            backup_root=workspace_config.backup_dir,
        )

        # 5) QQ 传输与 team 协作
        async def on_event(event: dict) -> None:
            await dispatcher.handle_onebot_event(event)

        transport = build_transport(settings, on_event)
        notifier = Notifier(
            transport=transport, authz=authz, redactor=redactor, settings=settings
        )
        executor = StepExecutor(
            tasks=tasks, store=workflow_store, authz=authz, settings=settings,
            redactor=redactor,
        )
        runner = WorkflowRunner(
            store=workflow_store, executor=executor, notifier=notifier, settings=settings
        )
        team_service = WorkflowService(
            store=workflow_store, runner=runner, registry=registry_view,
            settings=settings, authz=authz, rate_limiter=rate_limiter,
            tasks=tasks, workspaces=workspaces, confirmations=confirmations,
        )
        team_handler = TeamCommandHandler(
            service=team_service, notifier=notifier, settings=settings
        )

        # 6) 命令注册表 + preset
        commands = CommandRegistry()
        presets = presets_from_config(
            commands_config.get("presets") if isinstance(commands_config, dict) else None
        )
        commands.register_presets(presets)

        dispatcher = Dispatcher(
            settings=settings, registry=backends, store=store, authz=authz,
            rate_limiter=rate_limiter, confirmations=confirmations, transport=transport,
            redactor=redactor, tasks=tasks, workspaces=workspaces, acl=acl,
            commands=commands, presets=presets, team=team_handler,
        )
        commands.verify()

        if isinstance(transport, OneBotHTTPTransport):
            transport.install(app)

        # 7) HTTP API（Token 映射可信身份；未配置 Token 时只保留健康接口）
        try:
            app.state.api_tokens = build_identity_table(settings.console_api or {})
        except ValueError as exc:
            logger.error("API Token 配置非法，已禁用 /api/v1：%s", exc)
            app.state.api_tokens = {}
        app.state.background = set()
        app.state.settings = settings
        app.state.dispatcher = dispatcher
        app.state.tasks = tasks
        app.state.workspaces = workspaces
        app.state.backends = backends
        app.state.workflows = workflow_store
        app.state.workflow_runner = runner
        app.state.workspace_config = workspace_config

        for note in list(backends.notes) + list(workspace_config.notes):
            logger.warning("配置说明：%s", note)
        logger.info(
            "启动完成：transport=%s，已配置 Agent=%s，授权根=%d，API Token=%d，"
            "中断的团队任务=%d、任务=%d",
            settings.transport,
            [agent["name"] for agent in backends.agent_discovery() if agent["configured"]] or "（无）",
            len(workspace_config.grants), len(app.state.api_tokens),
            interrupted_teams, interrupted_tasks,
        )
        await transport.start()
        try:
            yield
        finally:
            for job in list(app.state.background):
                job.cancel()
            if app.state.background:
                import asyncio

                await asyncio.gather(*app.state.background, return_exceptions=True)
            await runner.shutdown()
            await transport.stop()
            store.close()
            await registry_view.aclose()
            logger.info("已停止")

    app = FastAPI(title="QQ 多模型统一控制台", version="2.0.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    async def status(request: Request) -> dict:
        """健康与状态接口（兼容既有部署；不是旧 QQ 口令）。"""
        dispatcher: Dispatcher | None = getattr(request.app.state, "dispatcher", None)
        if dispatcher is None:
            return {"status": "starting"}
        backends: BackendRegistry = dispatcher.registry
        workspace_config = getattr(request.app.state, "workspace_config", None)
        return {
            "status": "ok",
            "schema_version": settings.schema_version,
            "transport": type(dispatcher.transport).__name__,
            "agents": backends.agent_discovery(),
            "backends": [
                {
                    "id": item.id, "adapter_type": item.adapter_type,
                    "enabled": item.enabled, "configured": item.configured,
                    "native_modes": sorted(item.native_modes),
                    "broker_modes": sorted(item.broker_modes),
                    "capabilities": sorted(item.capabilities()),
                    "isolation": item.isolation,
                    "unavailable_reason": item.unavailable_reason,
                }
                for item in backends.descriptors()
            ],
            "commands": [spec.name for spec in dispatcher.commands.all_specs()],
            "presets": sorted(dispatcher.presets),
            "workspaces": {
                "roots": (
                    dispatcher.acl.describe_grants() if dispatcher.acl else []
                ),
                "notes": list(getattr(workspace_config, "notes", ()) or ()),
                "max_workers": getattr(workspace_config, "max_workers", 1),
                "cross_process_lock": getattr(workspace_config, "cross_process_lock", False),
                "active_locks": (
                    dispatcher.workspaces._coordinator.active_locks()  # noqa: SLF001
                ),
            },
            "config_notes": list(backends.notes),
            "pending_confirmations": dispatcher.confirmations.pending_count(),
            "api_v1_enabled": bool(getattr(request.app.state, "api_tokens", {})),
            "limits": {
                "request_timeout_seconds": settings.request_timeout_seconds,
                "max_context_turns": settings.max_context_turns,
                "max_message_length": settings.max_message_length,
                "rate_limit_per_minute": settings.rate_limit_per_minute,
                "team_enabled": settings.team_enabled,
                "team_max_steps": settings.team_max_steps,
                "team_max_active_per_user": settings.team_max_active_per_user,
                "team_max_active_global": settings.team_max_active_global,
                "team_max_parallel_steps": settings.team_max_parallel_steps,
                "team_workflow_timeout_seconds": settings.team_workflow_timeout_seconds,
                "team_step_timeout_seconds": settings.team_step_timeout(),
            },
        }

    app.include_router(create_router(lambda target: target.state))
    return app


def _app_state(request_app):
    return request_app.state


app = create_app()


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        app,
        host=settings.http_listen_host,
        port=settings.http_listen_port,
        log_config=None,  # 保留我们自己的日志装配（含密钥脱敏）
    )


if __name__ == "__main__":
    main()
