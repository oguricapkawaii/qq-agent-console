"""核心数据模型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


def new_task_id() -> str:
    """生成短 task_id，用于回复展示、日志关联与审批绑定。"""
    return uuid.uuid4().hex[:8]


@dataclass(slots=True)
class InboundMessage:
    """一条来自 QQ 群的文本消息（已由传输层提取纯文本）。"""

    group_id: int
    user_id: int
    self_id: int
    text: str
    message_id: int | None = None
    nickname: str | None = None


@dataclass(slots=True)
class ChatMessage:
    """发给模型的一条消息。"""

    role: str  # system / user / assistant
    content: str


@dataclass(slots=True)
class AgentReply:
    """适配器的返回。error 为简短、已脱敏的错误类别，绝不含密钥/请求头/堆栈。"""

    content: str = ""
    error: str | None = None
    # 模型的思考过程（可选，如 ZCode 的 reasoning 部分）；仅供展示，不进入上下文。
    thinking: str = ""
    # 工具调用摘要（可选，如 ZCode 文件工具）；仅供展示与审计，不进入上下文。
    tool_log: list[str] = field(default_factory=list)
    # L01：失败诊断（已脱敏、有界）。例如本机 CLI 的 stderr 摘要与能力探测结论，
    # 把"只显示 codex_failed"变成可操作的信息；绝不包含密钥。
    diagnostic: str = ""
    # L04：适配器**实际使用**的推理强度。调用方没显式指定时由后端的模式化默认决定，
    # 必须如实回填，任务记录里才不会把"实际是 low"显示成"未指定"。
    resolved_effort: str | None = None


@dataclass(slots=True)
class Command:
    """解析后的命令。"""

    name: str
    args: str


@dataclass(slots=True)
class TaskResult:
    """一次模型调用任务的结果。"""

    task_id: str
    agent_name: str
    agent_display: str
    ok: bool
    content: str = ""
    error: str | None = None
    duration_ms: int = 0
    model: str = ""
    effort: str | None = None
    thinking: str = ""
    tool_log: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InvocationOptions:
    """一次调用的模型选择与能力声明。

    ``mode`` 是权威的能力来源（text/read/edit/exec）；``workspace_tools`` 保留为
    原生后端"是否启用本机工具"的开关，由 mode 推导，不再是独立业务开关。
    """

    model: str
    effort: str | None = None
    directory: str | None = None
    workspace_tools: bool = True
    mode: str = "text"
    backend: str | None = None

    @property
    def wants_workspace(self) -> bool:
        return self.mode in {"read", "edit", "exec"}

    def with_mode(self, mode: str) -> "InvocationOptions":
        return InvocationOptions(
            model=self.model, effort=self.effort, directory=self.directory,
            workspace_tools=mode in {"read", "edit", "exec"}, mode=mode,
            backend=self.backend,
        )

