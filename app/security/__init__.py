"""安全组件：鉴权、目录 ACL、确认服务、限流、脱敏、文本消毒、受控命令策略。"""

from app.security.acl import AclService, RootGrant, mode_allows
from app.security.approval import (
    ApprovalOutcome,
    ApprovalProposal,
    ApprovalStore,
    InMemoryApprovalStore,
)
from app.security.authz import Authorizer
from app.security.confirmations import (
    Confirmation,
    ConfirmationKind,
    ConfirmationOutcome,
    ConfirmationStore,
    InMemoryConfirmationStore,
    PendingAction,
)
from app.security.exec_policy import ExecDenied, ExecPolicy, ExecResult, run_command
from app.security.rate_limit import SlidingWindowRateLimiter
from app.security.redaction import RedactingFilter, SecretRedactor
from app.security.text_safety import sanitize_inbound_text, sanitize_outbound_text, split_message

__all__ = [
    "AclService",
    "ApprovalOutcome",
    "ApprovalProposal",
    "ApprovalStore",
    "Authorizer",
    "Confirmation",
    "ConfirmationKind",
    "ConfirmationOutcome",
    "ConfirmationStore",
    "ExecDenied",
    "ExecPolicy",
    "ExecResult",
    "InMemoryApprovalStore",
    "InMemoryConfirmationStore",
    "PendingAction",
    "RedactingFilter",
    "RootGrant",
    "SecretRedactor",
    "SlidingWindowRateLimiter",
    "mode_allows",
    "run_command",
    "sanitize_inbound_text",
    "sanitize_outbound_text",
    "split_message",
]
