"""AgentAdapter 接口：所有模型后端必须实现此抽象。

分层约定（统一架构第 5 节）：
- Provider（API 协议/鉴权）/ Backend（API adapter 或 harness 执行器）/ Agent（用户
  选择的逻辑入口）分离；本类实现的是 **Backend adapter**；
- `configured` 表示该后端已启用且具备可用凭据；
- `native_modes` 如实声明后端自己实现、隔离可验证的能力；`broker_modes` 声明通过
  公共文件 broker 可获得的能力。目录能力**不依赖 agent 名字**，也不再由
  业务层写 `name == "codex"` 之类分支；
- `chat` 必须遵守传入的 timeout；失败时返回带简短、已脱敏 error 的 AgentReply
  （如 "timeout" / "network" / "api_error_500" / "not_configured"），
  绝不把 API 密钥、完整请求头或内部堆栈放进返回内容。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from app.models import AgentReply, ChatMessage, InvocationOptions

_ALL_MODES = (MODE_TEXT, MODE_READ, MODE_EDIT, MODE_EXEC)


class AgentAdapter(ABC):
    # 子类实例需覆盖以下类属性
    name: str = ""
    display_name: str = ""
    model_name: str = ""
    enabled: bool = True
    # /status 中展示的"未配置"原因（可选）
    status_note: str | None = None

    # ---- 能力声明（如实描述，不猜测）----
    adapter_type: str = "unknown"
    # broker：文件操作全部由公共 broker 完成；sandbox：原生工具在验证过的沙箱内
    isolation: str = "broker"
    native_modes: frozenset[str] = frozenset()
    broker_modes: frozenset[str] = frozenset({MODE_READ, MODE_EDIT})
    credential_env: str = ""
    notes: tuple[str, ...] = ()

    @property
    @abstractmethod
    def configured(self) -> bool:
        """是否已启用且具备可用凭据。"""

    @abstractmethod
    async def chat(self, messages: Sequence[ChatMessage], *, timeout: float) -> AgentReply:
        """执行一次对话补全。"""

    @property
    def model_options(self) -> dict[str, tuple[str, ...]]:
        return {self.model_name: ()} if self.model_name else {}

    # ---------- 能力 ----------

    def capabilities(self) -> frozenset[str]:
        """实际可用模式。

        BackendRegistry 在装配时会把「原生 ∩ broker ∩ 全局策略（例如 ExecPolicy）」
        的结果写回 `granted_modes`，确保 adapter 自检与 BackendDescriptor 一致。
        """
        granted = getattr(self, "granted_modes", None)
        if granted:
            return frozenset(granted)
        return frozenset({MODE_TEXT}) | self.native_modes | self.broker_modes

    def supports(self, mode: str) -> bool:
        return mode in self.capabilities()

    def capability_error(self, mode: str) -> str:
        if self.supports(mode):
            return ""
        if not self.enabled:
            return f"后端 {self.name} 已禁用。"
        if not self.configured:
            return f"后端 {self.name} 尚未配置：{self.status_note or '缺少配置'}"
        return (
            f"capability_not_supported：后端 {self.name} 不支持 {mode} 模式"
            f"（可用：{'/'.join(sorted(self.capabilities()))}）；不会偷偷换后端或降级模式。"
        )

    # ---------- 校验 ----------

    def validate_options(self, options: InvocationOptions) -> None:
        mode = options.mode or MODE_TEXT
        if mode not in _ALL_MODES:
            raise ValueError(f"未知模式：{mode}")
        error = self.capability_error(mode)
        if error:
            raise ValueError(error)
        if options.model not in self.model_options:
            raise ValueError(
                f"模型不在后端 {self.name} 的可选列表中，请用 /agent models {self.name} 查看。"
            )
        if options.effort is not None and options.effort not in self.model_options[options.model]:
            raise ValueError(
                f"该模型不支持此推理强度，请用 /agent models {self.name} 查看。"
            )
        if mode != MODE_TEXT and not options.directory:
            raise ValueError(
                f"{mode} 模式需要目录；目录由 WorkspaceService 解析后写入 options.directory。"
            )

    async def chat_with_options(self, messages: Sequence[ChatMessage], *, timeout: float,
                                options: InvocationOptions) -> AgentReply:
        self.validate_options(options)
        return await self.chat(messages, timeout=timeout)

    async def aclose(self) -> None:
        """释放后端资源；默认无操作。"""
        return None
