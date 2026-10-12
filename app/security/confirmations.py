"""统一确认服务：目录候选选择与已明确列出内容的受控操作确认。

设计要点：
- 确认码由应用程序用 secrets 生成，模型不能生成、也不能自行确认；
- 每条确认绑定「类型 + 群 + 发起人 + 关联任务/草稿版本 + 摘要 + TTL」；
- 一次性使用：确认成功即失效；过期、重放、跨群、跨用户一律拒绝；
- 目录选择的序号只能落在该确认记录的候选快照内，不能绑定别人的任务。

`ApprovalStore` 旧接口（app/security/approval.py）保留为同一实现的视图，
避免出现两套并行的一次性确认机制。
"""

from __future__ import annotations

import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ConfirmationOutcome(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    WRONG_USER = "wrong_user"
    WRONG_SCOPE = "wrong_scope"
    WRONG_KIND = "wrong_kind"


# 旧名称别名：同一份实现，避免第二套机制
ApprovalOutcome = ConfirmationOutcome


class ConfirmationKind:
    """确认类型常量。"""

    WORKSPACE_SELECT = "workspace.select"
    ACTION = "action.confirm"
    FILE_CHANGES = "file.changes"

    ALL = (WORKSPACE_SELECT, ACTION, FILE_CHANGES)


@dataclass(slots=True)
class PendingAction:
    """确认通过后要执行的动作。

    确认层只保管、不解释：`kind` 与 `payload` 的含义由调用方定义，
    因此新增一种受控操作不需要改动确认机制本身。
    """

    kind: str
    payload: dict
    summary: str


@dataclass(slots=True)
class Confirmation:
    """一条待确认记录。"""

    code: str
    kind: str
    user_id: int
    group_id: int | None
    summary: str
    expires_at: float  # time.monotonic() 时间戳
    task_id: str = ""
    draft_version: int = 0
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0
    # 一次性消费后的动作（仅 ACTION 类需要）
    action: PendingAction | None = None

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) > self.expires_at

    def ttl_remaining(self, now: float | None = None) -> int:
        current = now if now is not None else time.monotonic()
        return max(0, int(self.expires_at - current))


class ConfirmationStore(ABC):
    """确认存储接口；未来可替换为 SQLite 实现（重启即失效是当前契约）。"""

    @abstractmethod
    def create(
        self, *, kind: str, user_id: int, summary: str, payload: dict | None = None,
        group_id: int | None = None, task_id: str = "", draft_version: int = 0,
        ttl_seconds: float | None = None, action: PendingAction | None = None,
    ) -> Confirmation:
        """创建一条有 TTL 的一次性确认记录。"""

    @abstractmethod
    def consume(
        self, *, kind: str, user_id: int, code: str, group_id: int | None = None
    ) -> tuple[ConfirmationOutcome, Confirmation | None]:
        """校验并消费确认码；成功时返回记录本身（含候选快照/动作）。"""

    @abstractmethod
    def pending_count(self) -> int:
        """当前待确认数量。"""


class InMemoryConfirmationStore(ConfirmationStore):
    def __init__(self, default_ttl_seconds: float = 300.0) -> None:
        self._default_ttl = default_ttl_seconds
        self._pending: dict[str, Confirmation] = {}

    # ---------- 新接口 ----------

    def create(
        self, *, kind: str, user_id: int, summary: str, payload: dict | None = None,
        group_id: int | None = None, task_id: str = "", draft_version: int = 0,
        ttl_seconds: float | None = None, action: PendingAction | None = None,
    ) -> Confirmation:
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        code = self._new_code()
        record = Confirmation(
            code=code,
            kind=kind,
            user_id=user_id,
            group_id=group_id,
            summary=summary,
            expires_at=time.monotonic() + ttl,
            task_id=task_id,
            draft_version=draft_version,
            payload=dict(payload or {}),
            created_at=time.monotonic(),
            action=action,
        )
        self._pending[code] = record
        return record

    def consume(
        self, *, kind: str, user_id: int, code: str, group_id: int | None = None
    ) -> tuple[ConfirmationOutcome, Confirmation | None]:
        return self._consume(kind=kind, user_id=user_id, code=code, group_id=group_id)

    def _consume(
        self, *, kind: str, user_id: int, code: str, group_id: int | None = None
    ) -> tuple[ConfirmationOutcome, Confirmation | None]:
        normalized = (code or "").strip().upper()
        record = self._pending.get(normalized)
        if record is None:
            return ConfirmationOutcome.NOT_FOUND, None
        if record.expired():
            del self._pending[normalized]
            return ConfirmationOutcome.EXPIRED, None
        if record.user_id != user_id:
            return ConfirmationOutcome.WRONG_USER, None
        if record.kind != kind:
            return ConfirmationOutcome.WRONG_KIND, None
        if record.group_id is not None and group_id is not None and record.group_id != group_id:
            return ConfirmationOutcome.WRONG_SCOPE, None
        del self._pending[normalized]  # 一次性使用
        return ConfirmationOutcome.OK, record

    def consume_record(
        self, *, kind: str, user_id: int, code: str, group_id: int | None = None
    ) -> tuple[ConfirmationOutcome, Confirmation | None]:
        """显式命名的记录版消费入口（供需要候选快照的调用方使用）。"""
        return self._consume(kind=kind, user_id=user_id, code=code, group_id=group_id)

    def peek(self, code: str) -> Confirmation | None:
        return self._pending.get((code or "").strip().upper())

    def invalidate(self, predicate) -> int:
        """使满足条件的确认失效（例如草稿被改动、权限被撤销）。"""
        doomed = [code for code, record in self._pending.items() if predicate(record)]
        for code in doomed:
            del self._pending[code]
        return len(doomed)

    def count_kind(self, kind: str) -> int:
        return sum(1 for record in self._pending.values() if record.kind == kind)

    def pending_count(self) -> int:
        return len(self._pending)

    # ---------- 旧 ApprovalStore 视图（同一份存储） ----------

    def propose(self, *, user_id: int, task_id: str, summary: str,
                ttl_seconds: float | None = None,
                action: PendingAction | None = None, group_id: int | None = None) -> Confirmation:
        return self.create(
            kind=ConfirmationKind.ACTION,
            user_id=user_id,
            group_id=group_id,
            task_id=task_id,
            summary=summary,
            ttl_seconds=ttl_seconds,
            action=action,
            payload=(action.payload if action is not None else {}),
        )

    def approve(self, *, user_id: int, code: str) -> ConfirmationOutcome:
        outcome, _ = self._consume(kind=ConfirmationKind.ACTION, user_id=user_id, code=code)
        return outcome

    def consume_action(
        self, *, user_id: int, code: str, group_id: int | None = None
    ) -> tuple[ConfirmationOutcome, PendingAction | None]:
        outcome, record = self._consume(
            kind=ConfirmationKind.ACTION, user_id=user_id, code=code, group_id=group_id
        )
        if outcome is not ConfirmationOutcome.OK or record is None:
            return outcome, None
        return outcome, record.action

    def _new_code(self) -> str:
        code = secrets.token_hex(4).upper()
        while code in self._pending:
            code = secrets.token_hex(4).upper()
        return code


class InMemoryApprovalStore(InMemoryConfirmationStore):
    """兼容视图：`consume` 返回 (结果, PendingAction)，语义与确认记录一致。"""

    def consume(self, *, user_id: int, code: str, group_id: int | None = None):  # type: ignore[override]
        return self.consume_action(user_id=user_id, code=code, group_id=group_id)


ApprovalStore = ConfirmationStore
