"""应用配置。

密钥只能来自环境变量（或可选的本地 YAML 配置文件），
绝不写入源码或示例配置的真实值中。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """全部配置项。环境变量优先级高于 YAML（CONFIG_FILE 指定的文件）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- QQ 权限（默认拒绝：不在名单内的用户只能 /help，不在名单内的群完全静默）----
    # NoDecode：环境变量按"逗号分隔"而不是 JSON 解析
    qq_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    qq_allowed_group_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # 需要忽略的用户（例如群内其他机器人），防止机器人互相回复
    qq_ignore_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    database_path: str = "data/bot.db"

    # ---- Agent：OpenAI 兼容（GPT / DeepSeek / GLM 各自独立配置）----
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_enabled: bool = True

    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_enabled: bool = True
    deepseek_backend: Literal["api", "harness"] = "api"
    deepseek_harness_binary: str = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
    deepseek_harness_base_url: str = "https://api.deepseek.com/anthropic"
    deepseek_harness_workdir: str = "."

    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_api_key: str = ""
    glm_model: str = "glm-4"
    glm_enabled: bool = True

    zcode_enabled: bool = False
    zcode_node: str = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin/node"
    zcode_entry: str = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
    zcode_builtin: str = "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json"
    zcode_model: str = "glm-5.3"
    zcode_catalog: str = "data/zcode_models.json"
    # 文件任务模式：模型可在该目录内读写文件（写操作经路径校验自动审批）；留空 = 纯文本模式
    zcode_workspace_root: str = ""

    # ---- Codex：本机 CLI 的受限文本调用 ----
    codex_enabled: bool = False
    codex_binary: str = "codex"
    codex_workdir: str = "."
    codex_model: str = ""
    # QQ显式--dir可选择这些根目录下的子目录；空列表关闭文件模式。
    codex_allowed_roots: list[str] = Field(default_factory=list)
    # API 选项须按账号/接口能力配置；默认只允许现有模型、不发送强度参数。
    glm_model_options: dict[str, list[str]] = Field(default_factory=dict)
    deepseek_model_options: dict[str, list[str]] = Field(default_factory=dict)
    openai_model_options: dict[str, list[str]] = Field(default_factory=dict)

    # ---- OneBot 接入 ----
    # onebot_ws: 本程序作为 WebSocket 客户端连接 OneBot 实现的 WS 服务端
    # onebot_http: 本程序提供 HTTP 事件上报接口，并通过 OneBot HTTP API 发送消息
    transport: str = "onebot_ws"
    onebot_ws_url: str = "ws://127.0.0.1:3001"
    onebot_http_api_url: str = "http://127.0.0.1:5700"
    onebot_access_token: str = ""
    http_listen_host: str = "127.0.0.1"
    http_listen_port: int = 8080

    # ---- 可靠性 / 限制 ----
    request_timeout_seconds: float = 60.0
    max_context_turns: int = 8
    # 单条 QQ 消息的最大长度（超出自动分段）
    max_message_length: int = 1500
    # 回复中附带思考过程的最大字符数（超出截断；0 表示不附带）
    max_thinking_chars: int = 800
    # 思考过程归档目录（相对工作目录；空字符串表示不归档）
    thinking_archive_dir: str = "runtime/state/thinking"
    # 共享线程：同一「群 × 用户」下所有 Agent 用同一条上下文，互相可见（Agent 间对话）。
    # 关闭时保持原样：每个 Agent 的上下文互相隔离。
    shared_agent_context: bool = False
    # 受限文件读取（/read）：白名单根目录；空字符串表示关闭该命令
    file_read_root: str = ""
    file_read_max_bytes: int = 512 * 1024
    # 受限文件写入（/write + /approve）：白名单根目录；空字符串表示关闭写操作
    file_write_root: str = ""
    file_write_max_bytes: int = 64 * 1024
    # 已废弃：新的写路径由 broker/ChangeSet 控制（覆盖前自动备份）。
    # 保留字段只为兼容旧配置，不参与任何权限判定。
    file_write_allow_overwrite: bool = False
    rate_limit_per_minute: int = 6
    approval_ttl_seconds: int = 300

    # ---- 多 Agent 协作（/team，V1 全部为无工具文本步骤）----
    team_enabled: bool = True
    # 单个计划允许的步骤数
    team_max_steps: int = 8
    # 同一用户跨群同时活动的团队任务数（queued/running）
    team_max_active_per_user: int = 1
    # 全局同时活动的团队任务数（queued/running）
    team_max_active_global: int = 4
    # 全局协作步骤并发上限
    team_max_parallel_steps: int = 2
    # 团队目标与单步指令的长度上限
    team_max_prompt_chars: int = 12000
    # 单步总输入字符上限（系统边界 + 目标 + 指令 + 直接上游答复）
    team_max_input_chars: int = 60000
    # 单步最终答复字符上限；超限按 output_limit 失败，不截断后交给下游
    team_max_output_chars: int = 30000
    # 单轮总时限（秒），覆盖排队与执行
    team_workflow_timeout_seconds: float = 1800.0
    # 单步模型调用时限（秒）；0 表示自动取 min(REQUEST_TIMEOUT_SECONDS, 300)
    team_step_timeout_seconds: float = 0.0

    log_level: str = "INFO"

    # ---- 统一控制台（schema_version 2）----
    # 结构化配置由 YAML 提供（YAML -> 环境变量 setdefault，真实环境变量优先），
    # 具体解析与冲突检测见 app/backends/registry.py 与 app/workspaces/configurator.py。
    # 2 = 统一控制台配置（backends/agents/workspaces/commands/api）；1 = 旧式 per-agent 配置
    schema_version: int = 2
    console_backends: dict[str, Any] = Field(default_factory=dict)
    console_agents: dict[str, Any] = Field(default_factory=dict)
    console_workspaces: dict[str, Any] = Field(default_factory=dict)
    console_commands: dict[str, Any] = Field(default_factory=dict)
    console_api: dict[str, Any] = Field(default_factory=dict)
    console_api_token_env: str = "QQ_CONSOLE_API_TOKEN"
    # 目录锁：进程内锁只保证单进程互斥；worker>1 时必须提供跨进程锁
    workspace_max_workers: int = 1
    workspace_cross_process_lock: bool = False
    workspace_lock_timeout_seconds: float = 30.0
    # 任务上下文：team 步骤不读写普通聊天历史
    team_context_isolation: bool = True

    @field_validator("workspace_max_workers", mode="after")
    @classmethod
    def _positive_workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WORKSPACE_MAX_WORKERS 必须为正整数")
        return value

    @field_validator(
        "team_max_steps",
        "team_max_active_per_user",
        "team_max_active_global",
        "team_max_parallel_steps",
        "team_max_prompt_chars",
        "team_max_input_chars",
        "team_max_output_chars",
        mode="after",
    )
    @classmethod
    def _positive_team_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("TEAM_* 限额必须为正整数")
        return value

    @field_validator("team_workflow_timeout_seconds", mode="after")
    @classmethod
    def _positive_team_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("TEAM_WORKFLOW_TIMEOUT_SECONDS 必须为有限正数")
        return value

    @field_validator("team_step_timeout_seconds", mode="after")
    @classmethod
    def _nonnegative_step_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("TEAM_STEP_TIMEOUT_SECONDS 必须为有限非负数")
        return value

    def team_step_timeout(self) -> float:
        """单步调用时限：未显式配置时取 min(REQUEST_TIMEOUT_SECONDS, 300)。"""
        if self.team_step_timeout_seconds > 0:
            return self.team_step_timeout_seconds
        return min(self.request_timeout_seconds, 300.0)

    @field_validator(
        "qq_allowed_user_ids",
        "qq_allowed_group_ids",
        "qq_ignore_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_id_list(cls, value: Any) -> Any:
        """允许环境变量使用逗号（或中文逗号/空格）分隔的 ID 列表。"""
        if isinstance(value, str):
            ids: list[int] = []
            for part in value.replace("，", ",").replace(" ", ",").split(","):
                part = part.strip()
                if part:
                    ids.append(int(part))
            return ids
        if isinstance(value, (list, tuple)):
            return [int(item) for item in value]
        return value


# YAML 配置文件中的路径 -> 环境变量名（真实环境变量优先级更高）
_YAML_TO_ENV: dict[tuple[str, ...], str] = {
    ("qq", "allowed_user_ids"): "QQ_ALLOWED_USER_IDS",
    ("qq", "allowed_group_ids"): "QQ_ALLOWED_GROUP_IDS",
    ("qq", "ignore_user_ids"): "QQ_IGNORE_USER_IDS",
    ("storage", "database_path"): "DATABASE_PATH",
    ("agents", "glm", "base_url"): "GLM_BASE_URL",
    ("agents", "glm", "api_key"): "GLM_API_KEY",
    ("agents", "glm", "model"): "GLM_MODEL",
    ("agents", "glm", "enabled"): "GLM_ENABLED",
    ("agents", "deepseek", "base_url"): "DEEPSEEK_BASE_URL",
    ("agents", "deepseek", "api_key"): "DEEPSEEK_API_KEY",
    ("agents", "deepseek", "model"): "DEEPSEEK_MODEL",
    ("agents", "deepseek", "enabled"): "DEEPSEEK_ENABLED",
    ("agents", "gpt", "base_url"): "OPENAI_BASE_URL",
    ("agents", "gpt", "api_key"): "OPENAI_API_KEY",
    ("agents", "gpt", "model"): "OPENAI_MODEL",
    ("agents", "gpt", "enabled"): "OPENAI_ENABLED",
    ("agents", "codex", "enabled"): "CODEX_ENABLED",
    ("agents", "codex", "binary"): "CODEX_BINARY",
    ("agents", "codex", "workdir"): "CODEX_WORKDIR",
    ("agents", "codex", "model"): "CODEX_MODEL",
    ("agents", "codex", "allowed_roots"): "CODEX_ALLOWED_ROOTS",
    ("agents", "glm", "model_options"): "GLM_MODEL_OPTIONS",
    ("agents", "deepseek", "model_options"): "DEEPSEEK_MODEL_OPTIONS",
    ("agents", "gpt", "model_options"): "OPENAI_MODEL_OPTIONS",
    ("agents", "deepseek", "backend"): "DEEPSEEK_BACKEND",
    ("agents", "deepseek", "harness_binary"): "DEEPSEEK_HARNESS_BINARY",
    ("agents", "deepseek", "harness_base_url"): "DEEPSEEK_HARNESS_BASE_URL",
    ("agents", "deepseek", "harness_workdir"): "DEEPSEEK_HARNESS_WORKDIR",
    ("agents", "zcode", "enabled"): "ZCODE_ENABLED",
    ("agents", "zcode", "node"): "ZCODE_NODE",
    ("agents", "zcode", "entry"): "ZCODE_ENTRY",
    ("agents", "zcode", "builtin"): "ZCODE_BUILTIN",
    ("agents", "zcode", "model"): "ZCODE_MODEL",
    ("agents", "zcode", "catalog"): "ZCODE_CATALOG",
    ("agents", "zcode", "workspace_root"): "ZCODE_WORKSPACE_ROOT",
    ("transport", "kind"): "TRANSPORT",
    ("transport", "onebot_ws_url"): "ONEBOT_WS_URL",
    ("transport", "onebot_http_api_url"): "ONEBOT_HTTP_API_URL",
    ("transport", "onebot_access_token"): "ONEBOT_ACCESS_TOKEN",
    ("transport", "http_listen_host"): "HTTP_LISTEN_HOST",
    ("transport", "http_listen_port"): "HTTP_LISTEN_PORT",
    ("limits", "request_timeout_seconds"): "REQUEST_TIMEOUT_SECONDS",
    ("limits", "max_context_turns"): "MAX_CONTEXT_TURNS",
    ("limits", "max_message_length"): "MAX_MESSAGE_LENGTH",
    ("limits", "max_thinking_chars"): "MAX_THINKING_CHARS",
    ("limits", "thinking_archive_dir"): "THINKING_ARCHIVE_DIR",
    ("limits", "shared_agent_context"): "SHARED_AGENT_CONTEXT",
    ("limits", "file_read_root"): "FILE_READ_ROOT",
    ("limits", "file_read_max_bytes"): "FILE_READ_MAX_BYTES",
    ("limits", "file_write_root"): "FILE_WRITE_ROOT",
    ("limits", "file_write_max_bytes"): "FILE_WRITE_MAX_BYTES",
    ("limits", "file_write_allow_overwrite"): "FILE_WRITE_ALLOW_OVERWRITE",
    ("limits", "rate_limit_per_minute"): "RATE_LIMIT_PER_MINUTE",
    ("limits", "approval_ttl_seconds"): "APPROVAL_TTL_SECONDS",
    ("team", "enabled"): "TEAM_ENABLED",
    ("team", "max_steps"): "TEAM_MAX_STEPS",
    ("team", "max_active_per_user"): "TEAM_MAX_ACTIVE_PER_USER",
    ("team", "max_active_global"): "TEAM_MAX_ACTIVE_GLOBAL",
    ("team", "max_parallel_steps"): "TEAM_MAX_PARALLEL_STEPS",
    ("team", "max_prompt_chars"): "TEAM_MAX_PROMPT_CHARS",
    ("team", "max_input_chars"): "TEAM_MAX_INPUT_CHARS",
    ("team", "max_output_chars"): "TEAM_MAX_OUTPUT_CHARS",
    ("team", "workflow_timeout_seconds"): "TEAM_WORKFLOW_TIMEOUT_SECONDS",
    ("team", "step_timeout_seconds"): "TEAM_STEP_TIMEOUT_SECONDS",
    ("schema_version",): "SCHEMA_VERSION",
    ("backends",): "CONSOLE_BACKENDS",
    ("agents",): "CONSOLE_AGENTS",
    ("workspaces",): "CONSOLE_WORKSPACES",
    ("commands",): "CONSOLE_COMMANDS",
    ("api",): "CONSOLE_API",
    ("server", "host"): "HTTP_LISTEN_HOST",
    ("server", "port"): "HTTP_LISTEN_PORT",
    ("server", "api_token_env"): "CONSOLE_API_TOKEN_ENV",
    ("server", "workspace_max_workers"): "WORKSPACE_MAX_WORKERS",
    ("server", "workspace_cross_process_lock"): "WORKSPACE_CROSS_PROCESS_LOCK",
    ("server", "workspace_lock_timeout_seconds"): "WORKSPACE_LOCK_TIMEOUT_SECONDS",
    ("logging", "level"): "LOG_LEVEL",
}


def apply_yaml_config(path: str | Path, *, skip: frozenset[str] = frozenset()) -> None:
    """读取 YAML 配置并以最低优先级写入环境变量。

    不覆盖真实环境变量（setdefault），也不覆盖 `skip` 中列出的键（通常是 .env 里
    已声明的键），从而保证「真实环境变量 > .env > YAML」。
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for keys, env_name in _YAML_TO_ENV.items():
        if env_name in skip:
            continue
        node: Any = data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if node is None:
            continue
        if isinstance(node, bool):
            value = "true" if node else "false"
        elif isinstance(node, dict):
            value = json.dumps(node)
        elif env_name == "CODEX_ALLOWED_ROOTS":
            value = json.dumps(node)
        elif isinstance(node, (list, tuple)):
            value = ",".join(str(item) for item in node)
        else:
            value = str(node)
        os.environ.setdefault(env_name, value)


def read_env_file(path: str | Path = ".env") -> dict[str, str]:
    """读取 .env 的键值（不写入进程环境）。

    存在的意义：文档承诺的优先级是「真实环境变量 > .env > YAML」，而 YAML 是通过
    `os.environ.setdefault` 注入的。只有先知道 .env 里有哪些键，才能让 YAML 不去
    覆盖它们（否则 YAML 注入的 "环境变量" 会盖住 .env）。
    """
    env_path = Path(path)
    if not env_path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - python-dotenv 由 pydantic-settings 提供
        return {}
    return {
        key: value for key, value in dotenv_values(env_path).items()
        if key and value is not None
    }


def load_settings() -> Settings:
    """按「真实环境变量 > .env > YAML」装配配置。

    - .env 由 pydantic-settings 读取（含后端凭据），后端工厂通过
      `BackendConfig.credentials` 拿到这份值，因此 .env 里的密钥同样生效；
    - YAML 只注入"真实环境变量与 .env 都没有"的键，不再悄悄盖住 .env。
    """
    env_file = os.environ.get("ENV_FILE", ".env")
    env_values = read_env_file(env_file)
    config_file = os.environ.get("CONFIG_FILE", "").strip()
    if config_file and Path(config_file).is_file():
        apply_yaml_config(config_file, skip=frozenset(env_values))
    # 显式传入 env_file：读取的 .env 与上面用于判断优先级的必须是同一个文件
    return Settings(_env_file=env_file)
