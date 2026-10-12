"""统一执行协议：TaskRequest / ExecutionSpec / ExecutionResult。

HTTP、QQ 与 team 使用同一套结构（统一架构第 5 节），不为每种入口做独立插件框架。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.commands.options import MODE_TEXT

ENTRY_QQ = "qq"
ENTRY_HTTP = "http"
ENTRY_TEAM = "team"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """一次任务提交：可信身份 + 显式公共选项 + 正文。"""

    entry: str
    group_id: int
    user_id: int
    agent: str
    prompt: str
    mode: str = MODE_TEXT
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    effort_set: bool = False
    workspace: Path | None = None
    task_id: str = ""
    idempotency_key: str | None = None
    # team 元数据（单任务入口为空）
    team_id: str = ""
    step_id: str = ""
    round_no: int = 0
    # 上下文策略：普通聊天保留 shared_agent_context，team 不使用聊天历史
    use_chat_context: bool = True
    # 仅用于展示/审计的说明（不参与权限判定）
    origin: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """冻结后的执行规格：能力的交集在构造时已算清。"""

    task_id: str
    backend_id: str
    agent: str
    model: str
    effort: str | None
    mode: str
    workspace: Path | None = None
    # 冻结引用（授权版本、目录锁标识）
    grant_label: str = ""
    timeout_seconds: float = 60.0
    # 整个任务（含锁等待、所有模型轮次、工具调用）的硬期限；0 = 不限制
    deadline_seconds: float = 0.0
    max_input_chars: int = 60000
    max_output_chars: int = 30000
    use_broker: bool = False
    use_native: bool = False
    entry: str = ENTRY_QQ
    team_id: str = ""
    step_id: str = ""
    # 覆盖默认系统提示词（team 步骤使用隔离提示词）
    system_prompt: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class ChangeRecord:
    """一次文件变更的公开记录。"""

    path: str
    kind: str
    status: str
    before_hash: str = ""
    after_hash: str = ""
    backup: str = ""
    bytes: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path, "kind": self.kind, "status": self.status,
            "before_hash": self.before_hash, "after_hash": self.after_hash,
            "backup": self.backup, "bytes": self.bytes, "error": self.error,
        }


@dataclass(slots=True)
class ExecutionResult:
    """一次执行的真实结果；不得把"计划/建议"冒充为已发生的修改。"""

    ok: bool
    status: str = "failed"
    content: str = ""
    error: str | None = None
    detail: str = ""
    backend: str = ""
    agent: str = ""
    model: str = ""
    effort: str | None = None
    mode: str = MODE_TEXT
    duration_ms: int = 0
    thinking: str = ""
    tool_log: list[str] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    # F10：本次读取过的输入文件与哈希（team retry 判定输入漂移）
    input_fingerprints: dict[str, str] = field(default_factory=dict)
    partial: bool = False
    task_id: str = ""
    isolation: str = ""
    # V01：操作结果类别（changed/observed/executed/none/failed）与逐动作回执计数。
    # 判定只看动作、提交与命令回执，不看模型自述；QQ/HTTP/team 共用同一份取值。
    operation: str = ""
    action_counts: dict[str, int] = field(default_factory=dict)

    @property
    def changed_files(self) -> list[ChangeRecord]:
        return [change for change in self.changes if change.status == "applied"]

    def public_summary(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
            "mode": self.mode,
            "status": self.status,
            "ok": self.ok,
            "error": self.error,
            "operation": self.operation,
            "action_counts": dict(self.action_counts),
            "duration_ms": self.duration_ms,
            "changes": [change.as_dict() for change in self.changes],
            "partial": self.partial,
        }
