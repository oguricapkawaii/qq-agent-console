"""后端能力描述：如实说明原生能力、broker 能力与隔离等级。

Provider（API 协议/鉴权）— Backend（API adapter 或 harness 执行器）— Agent（用户
选择的逻辑入口）三层分离；BackendDescriptor 只描述事实，不猜测能力：
- `native_modes`：后端自己实现、且隔离可验证的模式（含 exec 才算支持 exec）；
- `broker_modes`：通过公共文件 broker 可获得的模式（只可能是 read/edit）；
- `isolation`：broker / sandbox / none，用于判断原生工具是否可用。

状态探测不产生模型调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT

# broker 可提供的模式：受控文件读写 + 受控命令执行（ExecPolicy 白名单）
BROKER_CAPABLE_MODES = frozenset({MODE_READ, MODE_EDIT, MODE_EXEC})
ALL_MODES = (MODE_TEXT, MODE_READ, MODE_EDIT, MODE_EXEC)

ISOLATION_BROKER = "broker"
ISOLATION_SANDBOX = "sandbox"
ISOLATION_NONE = "none"


@dataclass(frozen=True, slots=True)
class BackendDescriptor:
    """一个执行后端的公开描述。"""

    id: str
    agent: str
    adapter_type: str
    display_name: str
    enabled: bool
    configured: bool
    models: tuple[str, ...] = ()
    model_efforts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    default_model: str = ""
    native_modes: frozenset[str] = frozenset()
    broker_modes: frozenset[str] = frozenset()
    isolation: str = ISOLATION_BROKER
    # 是否配置了可验证的受控命令策略（broker 提供 exec 的前提）
    exec_policy: bool = False
    credential_env: str = ""
    unavailable_reason: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        bad = (self.native_modes | self.broker_modes) - set(ALL_MODES) - {MODE_TEXT}
        if bad:
            raise ValueError(f"后端 {self.id} 声明了未知模式：{sorted(bad)}")
        if MODE_EXEC in self.broker_modes and not self.exec_policy:
            raise ValueError(
                f"后端 {self.id} 声明 broker exec，但没有可验证的命令白名单；"
                "未配置 ExecPolicy 时不得声明 exec 能力。"
            )
        if self.isolation == ISOLATION_NONE and self.native_modes:
            raise ValueError(
                f"后端 {self.id} 隔离等级为 none，不能声明原生文件/执行能力；"
                "无法安全隔离时必须禁用原生工具或返回 capability_not_supported。"
            )

    # ---------- 能力查询 ----------

    def capabilities(self) -> frozenset[str]:
        """实际可用模式：text 恒可用，其余为原生 ∪ broker。"""
        return frozenset({MODE_TEXT}) | self.native_modes | (self.broker_modes & BROKER_CAPABLE_MODES)

    def supports(self, mode: str) -> bool:
        return mode in self.capabilities()

    def mode_source(self, mode: str) -> str:
        if mode == MODE_TEXT:
            return "native"
        if mode in self.native_modes:
            return "native"
        if mode in self.broker_modes:
            return "broker"
        return "unsupported"

    def capability_error(self, mode: str) -> str:
        if self.supports(mode):
            return ""
        if not self.enabled:
            return f"后端 {self.id} 已禁用。"
        if not self.configured:
            return f"后端 {self.id} 尚未配置：{self.unavailable_reason or '缺少配置'}"
        supported = "/".join(sorted(self.capabilities()))
        return (
            f"capability_not_supported：后端 {self.id} 不支持 {mode} 模式"
            f"（可用：{supported}）；不会偷偷换后端或降级模式。"
        )

    def effective_modes(self) -> frozenset[str]:
        return self.capabilities()

    def discovery(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "adapter_type": self.adapter_type,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "configured": self.configured,
            "default_model": self.default_model,
            "models": list(self.models),
            "model_efforts": {key: list(value) for key, value in self.model_efforts.items()},
            "native_modes": sorted(self.native_modes),
            "broker_modes": sorted(self.broker_modes),
            "exec_policy": self.exec_policy,
            "capabilities": sorted(self.capabilities()),
            "isolation": self.isolation,
            "credential_env": self.credential_env,
            "unavailable_reason": self.unavailable_reason,
            "notes": list(self.notes),
        }
