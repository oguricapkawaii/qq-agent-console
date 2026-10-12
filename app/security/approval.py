"""人工审批机制（统一确认服务的一次性码视图）。

历史上这里是独立的审批实现；统一控制台把「目录候选选择」与「受控操作确认」
合并到 `app.security.confirmations`，本模块只保留原来的导入路径与命名，
避免出现两套并行的一次性确认机制。
"""

from __future__ import annotations

from app.security.confirmations import (
    ApprovalOutcome,
    ApprovalStore,
    Confirmation,
    ConfirmationKind,
    ConfirmationOutcome,
    InMemoryApprovalStore,
    InMemoryConfirmationStore,
    PendingAction,
)

# 旧命名兼容：ApprovalProposal 就是一条确认记录
ApprovalProposal = Confirmation

__all__ = [
    "ApprovalOutcome",
    "ApprovalProposal",
    "ApprovalStore",
    "Confirmation",
    "ConfirmationKind",
    "ConfirmationOutcome",
    "InMemoryApprovalStore",
    "InMemoryConfirmationStore",
    "PendingAction",
]
