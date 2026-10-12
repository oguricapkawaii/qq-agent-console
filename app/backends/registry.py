"""BackendRegistry：从配置构建后端与逻辑 Agent，并如实描述能力。

- Provider / Backend / Agent 三层分离；
- `from_settings` 支持两种来源：
  1. 新式 `backends:` / `agents:` 结构（schema_version 2）；
  2. 旧式 per-agent 配置（GLM_*、DEEPSEEK_*、ZCOD E_*、CODEX_*），自动迁移为等价后端；
- 新旧同时存在时**不静默选择**：新配置生效并输出"配置来源/冲突"说明，供 /status 与
  启动日志展示；
- 状态探测不产生任何模型调用。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agents.base import AgentAdapter
from app.backends.builtin import register_builtin_adapters
from app.backends.descriptor import (
    ISOLATION_BROKER,
    BackendDescriptor,
)
from app.backends.factory import AdapterFactoryError, BackendConfig, build_backend, known_adapters
from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ
from app.config import Settings

logger = logging.getLogger("qqbot.backends.registry")

_AGENT_ORDER = ("glm", "deepseek", "zcode", "gpt", "codex")


class BackendConfigError(ValueError):
    """配置非法；启动时抛出，避免"配错了但悄悄跑起来"。"""


@dataclass(slots=True)
class AgentRuntime:
    """用户可见的逻辑 Agent。"""

    name: str
    display_name: str
    default_backend: str
    allowed_backends: tuple[str, ...] = ()
    default_model: str = ""
    default_effort: str | None = None

    def allows(self, backend_id: str) -> bool:
        return not self.allowed_backends or backend_id in self.allowed_backends


@dataclass(slots=True)
class BackendRuntime:
    """一个已构造的后端实例及其描述。"""

    id: str
    agent: str
    adapter: AgentAdapter
    descriptor: BackendDescriptor
    credential_env: str = ""
    # 原始配置项（例如 native_tools），由网关在判定原生能力时读取
    options: dict = field(default_factory=dict)

    def models(self) -> tuple[str, ...]:
        return self.descriptor.models


@dataclass(slots=True)
class BackendRegistry:
    """后端与 Agent 的运行时注册表。"""

    backends: dict[str, BackendRuntime] = field(default_factory=dict)
    agents: dict[str, AgentRuntime] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    # ---------- 构造 ----------

    @classmethod
    def from_adapters(
        cls, adapters: Mapping[str, AgentAdapter], *, exec_policy_enabled: bool = False,
        notes: tuple[str, ...] = (),
    ) -> "BackendRegistry":
        """直接以适配器构建（测试与嵌入式装配使用；后端 id = agent 名）。"""
        registry = cls(notes=notes)
        for name, adapter in adapters.items():
            registry._add_backend(
                BackendConfig(
                    id=name,
                    adapter=getattr(adapter, "adapter_type", "unknown"),
                    options=dict(getattr(adapter, "runtime_options", {}) or {}),
                ),
                adapter, backend_id=name, agent=name, exec_policy_enabled=exec_policy_enabled,
            )
            registry.agents[name] = AgentRuntime(
                name=name,
                display_name=adapter.display_name or name,
                default_backend=name,
                allowed_backends=(name,),
                default_model=adapter.model_name,
            )
        return registry

    @classmethod
    def from_settings(cls, settings: Settings) -> "BackendRegistry":
        register_builtin_adapters()
        notes: list[str] = []
        backends_cfg = settings.console_backends or {}
        agents_cfg = settings.console_agents or {}
        exec_policy_enabled = _exec_policy_enabled(settings)

        credential_map = _credential_map(settings)
        if backends_cfg:
            configs = _parse_backends(backends_cfg, credentials=credential_map)
            notes.extend(_legacy_conflict_notes(settings, configs))
        else:
            configs, migration_notes = _legacy_backends(settings, credentials=credential_map)
            notes.extend(migration_notes)
            if _looks_like_new_agents_config(agents_cfg):
                notes.append(
                    "agents: 使用了新式字段但 backends: 为空；已按旧式 per-agent 配置构建后端，"
                    "请补充 backends: 声明以启用统一接入。"
                )

        registry = cls(notes=tuple(notes))
        for config in configs:
            try:
                adapter = build_backend(config)
            except AdapterFactoryError as exc:
                raise BackendConfigError(f"后端 {config.id}：{exc}") from None
            except (TypeError, ValueError) as exc:
                raise BackendConfigError(f"后端 {config.id} 构造失败：{exc}") from None
            registry._add_backend(
                config, adapter, backend_id=config.id,
                agent=config.agent or config.id, exec_policy_enabled=exec_policy_enabled,
            )

        # 逻辑 Agent：默认后端 + 允许的后端集合
        if agents_cfg:
            for name, raw in agents_cfg.items():
                if not isinstance(raw, Mapping):
                    raise BackendConfigError(f"agents.{name} 必须是映射")
                registry._register_agent_config(str(name), raw)
        for backend in registry.backends.values():
            registry.agents.setdefault(
                backend.agent,
                AgentRuntime(
                    name=backend.agent,
                    display_name=backend.descriptor.display_name,
                    default_backend=backend.id,
                    allowed_backends=(backend.id,),
                    default_model=backend.descriptor.default_model,
                ),
            )
        registry._validate_agents()
        return registry

    def _add_backend(
        self, config: BackendConfig, adapter: AgentAdapter, *, backend_id: str, agent: str,
        exec_policy_enabled: bool,
    ) -> None:
        if backend_id in self.backends:
            raise BackendConfigError(f"后端 id 重复：{backend_id}")
        models = adapter.model_options
        broker_modes = set(adapter.broker_modes) & {MODE_READ, MODE_EDIT}
        if exec_policy_enabled:
            # exec 由公共 broker 的受控命令策略提供（并非后端原生能力）
            broker_modes.add(MODE_EXEC)
        descriptor = BackendDescriptor(
            id=backend_id,
            agent=agent,
            adapter_type=adapter.adapter_type,
            display_name=adapter.display_name or agent,
            enabled=bool(adapter.enabled),
            configured=bool(adapter.configured),
            models=tuple(models),
            model_efforts={key: tuple(value) for key, value in models.items()},
            default_model=adapter.model_name,
            native_modes=frozenset(adapter.native_modes),
            broker_modes=frozenset(broker_modes),
            isolation=adapter.isolation or ISOLATION_BROKER,
            exec_policy=exec_policy_enabled,
            credential_env=config.credential_env or adapter.credential_env,
            unavailable_reason=(
                "" if adapter.configured else (adapter.status_note or "缺少配置")
            ),
            notes=tuple(config.notes) + tuple(getattr(adapter, "notes", ()) or ()),
        )
        # 把最终能力写回适配器：adapter 自检与 descriptor 使用同一份事实
        adapter.granted_modes = descriptor.capabilities()
        self.backends[backend_id] = BackendRuntime(
            id=backend_id, agent=agent, adapter=adapter, descriptor=descriptor,
            credential_env=descriptor.credential_env, options=dict(config.options),
        )

    def _register_agent_config(self, name: str, raw: Mapping[str, Any]) -> None:
        unknown = set(raw) - {
            "display_name", "default_backend", "allowed_backends", "default_model",
            "default_effort", "enabled", "credential_env", "base_url", "api_key",
            "model", "model_options", "adapter",
        }
        if unknown:
            raise BackendConfigError(
                f"agents.{name} 含未知字段：{', '.join(sorted(unknown))}"
            )
        default_backend = str(raw.get("default_backend") or "")
        if not default_backend:
            raise BackendConfigError(f"agents.{name} 必须声明 default_backend")
        allowed = raw.get("allowed_backends")
        agent = AgentRuntime(
            name=name,
            display_name=str(raw.get("display_name") or name),
            default_backend=default_backend,
            allowed_backends=tuple(str(item) for item in allowed) if allowed else (),
            default_model=str(raw.get("default_model") or raw.get("model") or ""),
            default_effort=(str(raw["default_effort"]) if raw.get("default_effort") else None),
        )
        self.agents[name] = agent

    def _validate_agents(self) -> None:
        for agent in self.agents.values():
            if agent.default_backend not in self.backends:
                raise BackendConfigError(
                    f"agents.{agent.name}.default_backend 指向未定义的后端：{agent.default_backend}；"
                    f"已定义：{', '.join(sorted(self.backends)) or '（无）'}"
                )
            for backend_id in agent.allowed_backends:
                if backend_id not in self.backends:
                    raise BackendConfigError(
                        f"agents.{agent.name}.allowed_backends 含未定义后端：{backend_id}"
                    )

    # ---------- 查询 ----------

    def get(self, backend_id: str) -> BackendRuntime | None:
        return self.backends.get(backend_id)

    def agent(self, name: str) -> AgentRuntime | None:
        return self.agents.get(name)

    def all_agent_names(self) -> list[str]:
        ordered = [name for name in _AGENT_ORDER if name in self.agents]
        extra = sorted(name for name in self.agents if name not in _AGENT_ORDER)
        return ordered + extra

    def all_agents(self) -> list[AgentRuntime]:
        return [self.agents[name] for name in self.all_agent_names()]

    def for_agent(self, name: str) -> list[BackendRuntime]:
        agent = self.agents.get(name)
        if agent is None:
            return []
        allowed = agent.allowed_backends or tuple(
            backend_id for backend_id, backend in self.backends.items() if backend.agent == name
        )
        return [self.backends[item] for item in allowed if item in self.backends]

    def resolve(self, agent_name: str, backend_id: str | None = None) -> tuple[BackendRuntime | None, str]:
        """为逻辑 Agent 选择后端；返回（后端, 错误说明）。"""
        agent = self.agents.get(agent_name)
        if agent is None:
            return None, f"未知 Agent：{agent_name}；可选：" + ", ".join(self.all_agent_names())
        chosen = backend_id or agent.default_backend
        if backend_id and not agent.allows(backend_id):
            return None, (
                f"Agent {agent_name} 不允许使用后端 {backend_id}；"
                f"允许：{', '.join(agent.allowed_backends) or '（默认后端）'}"
            )
        backend = self.backends.get(chosen)
        if backend is None:
            return None, f"未知后端：{chosen}"
        return backend, ""

    def configured_agents(self) -> list[AgentRuntime]:
        result = []
        for agent in self.all_agents():
            backend = self.backends.get(agent.default_backend)
            if backend is not None and backend.descriptor.configured:
                result.append(agent)
        return result

    def descriptors(self) -> list[BackendDescriptor]:
        return [self.backends[key].descriptor for key in self.backends]

    def discovery(self) -> list[dict]:
        return [descriptor.discovery() for descriptor in self.descriptors()]

    def agent_discovery(self) -> list[dict]:
        rows = []
        for agent in self.all_agents():
            backends = self.for_agent(agent.name)
            rows.append({
                "name": agent.name,
                "display_name": agent.display_name,
                "default_backend": agent.default_backend,
                "allowed_backends": [item.id for item in backends],
                "default_model": agent.default_model,
                "default_effort": agent.default_effort,
                "configured": any(item.descriptor.configured for item in backends),
                "capabilities": sorted({
                    mode for item in backends for mode in item.descriptor.capabilities()
                }),
            })
        return rows

    async def aclose(self) -> None:
        for backend in self.backends.values():
            aclose = getattr(backend.adapter, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception:  # pragma: no cover - 关闭失败不应阻断退出
                logger.warning("后端 %s 关闭失败", backend.id, exc_info=True)


# ---------- 配置解析 ----------


def _exec_policy_enabled(settings: Settings) -> bool:
    """是否提供受控命令策略（broker exec 的前提）。

    默认启用：ExecPolicy 自带保守白名单（测试/构建工具），并拒绝提权、联网、
    敏感路径参数；只有显式 `commands.exec.enabled: false` 才关闭。
    """
    commands = settings.console_commands or {}
    if not isinstance(commands, Mapping):
        return True
    exec_cfg = commands.get("exec")
    if exec_cfg is None:
        return True
    if isinstance(exec_cfg, Mapping):
        return bool(exec_cfg.get("enabled", True))
    return bool(exec_cfg)


_LEGACY_FIELDS = (
    "openai_base_url", "openai_api_key", "openai_model", "openai_enabled",
    "deepseek_base_url", "deepseek_api_key", "deepseek_model", "deepseek_enabled",
    "deepseek_backend", "deepseek_harness_binary", "deepseek_harness_workdir",
    "glm_base_url", "glm_api_key", "glm_model", "glm_enabled",
    "zcode_enabled", "zcode_node", "zcode_entry", "zcode_builtin", "zcode_model",
    "zcode_catalog", "zcode_workspace_root",
    "codex_enabled", "codex_binary", "codex_workdir", "codex_model", "codex_allowed_roots",
)


def _legacy_overrides(settings: Settings) -> dict[str, Any]:
    """返回显式设置过（非默认值）的旧式字段，用于冲突说明。"""
    changed: dict[str, Any] = {}
    for name in _LEGACY_FIELDS:
        field = type(settings).model_fields.get(name)
        if field is None:
            continue
        current = getattr(settings, name)
        default = field.default
        if current != default:
            changed[name] = current
    return changed


def _legacy_conflict_notes(settings: Settings, configs: list[BackendConfig]) -> list[str]:
    changed = _legacy_overrides(settings)
    if not changed:
        return []
    configured_agents = {config.agent or config.id for config in configs}
    notes = [
        "检测到新式 backends: 配置与旧式 per-agent 配置同时存在；"
        "已按新式 backends: 构建后端（新配置优先），旧字段不再生效。",
        "受影响的旧字段：" + "、".join(sorted(changed)),
    ]
    for name, value in sorted(changed.items()):
        if name in {"glm_api_key", "deepseek_api_key", "openai_api_key"}:
            value = "（已设置，值不展示）"
        notes.append(f"  · {name} = {value}")
    notes.append("请把这些值迁移到 backends:/agents: 后删除旧字段，避免两套权限来源并存。")
    return notes


def _credential_map(settings: Settings) -> dict[str, str]:
    """把 Settings 与 .env 里的凭据映射为「环境变量名 -> 值」。

    pydantic-settings 读取 .env 后并不会写回 os.environ，因此后端工厂不能只看
    进程环境变量，否则"密钥写在 .env 里"的既有部署会被判定为未配置。
    除三个旧式字段外，还纳入 .env 中声明的**任意**键，这样自定义后端
    （例如 `credential_env: CORP_LLM_KEY`）只把密钥写进 .env 也能生效。
    """
    from app.config import read_env_file

    mapping: dict[str, str] = {
        "DEEPSEEK_API_KEY": settings.deepseek_api_key,
        "GLM_API_KEY": settings.glm_api_key,
        "OPENAI_API_KEY": settings.openai_api_key,
    }
    import os

    for key, value in read_env_file(os.environ.get("ENV_FILE", ".env")).items():
        mapping.setdefault(key, value)
    return {key: str(value) for key, value in mapping.items() if value}


def _parse_backends(
    raw: Mapping[str, Any], *, credentials: Mapping[str, str] | None = None
) -> list[BackendConfig]:
    configs: list[BackendConfig] = []
    for backend_id, value in raw.items():
        if not isinstance(value, Mapping):
            raise BackendConfigError(f"backends.{backend_id} 必须是映射")
        adapter = str(value.get("adapter") or "")
        if not adapter:
            raise BackendConfigError(
                f"backends.{backend_id} 必须声明 adapter；已登记类型："
                + ", ".join(known_adapters())
            )
        options = {
            key: item for key, item in value.items()
            if key not in {
                "adapter", "agent", "display_name", "enabled", "credential_env",
                "default_model", "model_options", "notes",
            }
        }
        configs.append(BackendConfig(
            id=str(backend_id),
            adapter=adapter,
            agent=str(value.get("agent") or backend_id),
            display_name=str(value.get("display_name") or backend_id),
            enabled=bool(value.get("enabled", True)),
            credential_env=str(value.get("credential_env") or ""),
            default_model=(str(value["default_model"]) if value.get("default_model") else None),
            model_options=value.get("model_options") or None,
            options=options,
            notes=tuple(str(item) for item in (value.get("notes") or ())),
            credentials=dict(credentials or {}),
        ))
    return configs


def _looks_like_new_agents_config(raw: Mapping[str, Any]) -> bool:
    for value in (raw or {}).values():
        if isinstance(value, Mapping) and {"default_backend", "allowed_backends"} & set(value):
            return True
    return False


def _legacy_backends(
    settings: Settings, *, credentials: Mapping[str, str] | None = None
) -> tuple[list[BackendConfig], list[str]]:
    """把旧式 per-agent 配置迁移为后端配置（等价能力，保守默认）。"""
    credentials = dict(credentials or {})
    configs: list[BackendConfig] = []
    notes: list[str] = []
    configs.append(BackendConfig(
        id="glm_api", adapter="openai_compatible", agent="glm", display_name="GLM",
        enabled=settings.glm_enabled, credential_env="GLM_API_KEY",
        default_model=settings.glm_model,
        model_options=settings.glm_model_options or None,
        options={"base_url": settings.glm_base_url},
        notes=("由旧式 GLM_* 配置迁移。",),
        credentials=credentials,
    ))
    if settings.deepseek_backend == "harness":
        configs.append(BackendConfig(
            id="deepseek_harness", adapter="deepseek_harness", agent="deepseek",
            display_name="DeepSeek Harness",
            enabled=settings.deepseek_enabled, credential_env="DEEPSEEK_API_KEY",
            default_model=settings.deepseek_model,
            options={
                "binary": settings.deepseek_harness_binary,
                "workdir": settings.deepseek_harness_workdir,
                "base_url": settings.deepseek_harness_base_url,
            },
            notes=("由旧式 DEEPSEEK_BACKEND=harness 配置迁移。",),
            credentials=credentials,
        ))
    else:
        configs.append(BackendConfig(
            id="deepseek_api", adapter="openai_compatible", agent="deepseek",
            display_name="DeepSeek", enabled=settings.deepseek_enabled,
            credential_env="DEEPSEEK_API_KEY", default_model=settings.deepseek_model,
            model_options=settings.deepseek_model_options or None,
            options={"base_url": settings.deepseek_base_url},
            notes=("由旧式 DEEPSEEK_BACKEND=api 配置迁移。",),
            credentials=credentials,
        ))
    configs.append(BackendConfig(
        id="gpt_api", adapter="openai_compatible", agent="gpt", display_name="GPT",
        enabled=settings.openai_enabled, credential_env="OPENAI_API_KEY",
        default_model=settings.openai_model,
        model_options=settings.openai_model_options or None,
        options={"base_url": settings.openai_base_url},
        notes=("由旧式 OPENAI_* 配置迁移。",),
        credentials=credentials,
    ))
    configs.append(BackendConfig(
        id="zcode", adapter="zcode_stdio", agent="zcode", display_name="ZCode",
        enabled=settings.zcode_enabled, credential_env="GLM_API_KEY",
        default_model=settings.zcode_model,
        options={
            "node": settings.zcode_node, "entry": settings.zcode_entry,
            "builtin": settings.zcode_builtin, "catalog": settings.zcode_catalog,
            "base_url": settings.glm_base_url,
            "workspace_root": settings.zcode_workspace_root,
        },
        notes=("由旧式 ZCODE_* 配置迁移。",),
        credentials=credentials,
    ))
    configs.append(BackendConfig(
        id="codex", adapter="codex_cli", agent="codex", display_name="Codex",
        enabled=settings.codex_enabled, credential_env="CODEX_HOME",
        default_model=settings.codex_model,
        options={
            "binary": settings.codex_binary, "workdir": settings.codex_workdir,
            "allowed_roots": list(settings.codex_allowed_roots),
        },
        notes=("由旧式 CODEX_* 配置迁移。",),
        credentials=credentials,
    ))
    if settings.codex_allowed_roots:
        notes.append(
            "旧式 CODEX_ALLOWED_ROOTS 已迁移为目录 ACL 的 admin_modes=read/edit/exec 授权；"
            "迁移报告见 /status 的 workspaces 段落。"
        )
    return configs, notes
