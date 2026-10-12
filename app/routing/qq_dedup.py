"""QQ 任务提交去重：带命名空间的稳定标识（QQ 消息重放最小整改）。

设计要点
--------
- **命名空间**：``v1|qq|<self_id>|<group_id>|<user_id>|<message_id>|<target>``。
  机器人（``self_id``）、群、用户与提交目标（单 Agent / all 子任务 / preset）全部入键，
  因此不同机器人、不同群、不同用户、不同子任务目标互不影响；
  HTTP 入口使用 ``v1|http|...`` 前缀（见 ``app/api/v1.py``），两类键**不会**混用。
- **入口/方案归属**：HTTP 新记录额外写 ``key_scheme="http:v1"``；升级前的旧记录该列为
  NULL。查询同时按 ``entry`` 与方案过滤，所以"字面等于新键的原始 key"与"字面等于旧原始
  键的新键"都不会跨归属冒名命中。
- **同一 message_id 的同文重放**：键相同 → 判定为重复；
  **不同 message_id 的同文请求**：键不同 → 仍然创建两个任务（不按正文哈希合并）。
- **没有 message_id**：不生成键（保持兼容：不做去重，行为与本轮整改前一致）。
- 本模块只负责**构造键与描述重复**，原子登记与并发裁决由
  ``TaskStore.register_idempotent_task()`` 借助唯一索引 ``idx_tasks_idem`` 完成。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import InboundMessage
from app.tasks.store import idempotency_key_variants

# 键版本：将来改变语义时递增，避免与新老键误判为同一条
QQ_DEDUP_KEY_VERSION = "v1"
QQ_DEDUP_ENTRY = "qq"
# 提交目标（target）的固定前缀
TARGET_TASK = "task"
TARGET_ALL = "all"
TARGET_PRESET = "preset"


@dataclass(frozen=True, slots=True)
class QQSubmissionKey:
    """一次 QQ 提交的稳定标识；``value`` 为空串表示"本次不去重"。"""

    value: str
    reason: str
    message_id: int | None
    target: str

    @property
    def deduplicated(self) -> bool:
        return bool(self.value)


def build_key(
    message: InboundMessage, *, target: str, agent: str, preset: str = "",
    bot_account: int | None = None,
) -> QQSubmissionKey:
    """为一次 QQ 提交构造带命名空间的稳定键。

    ``target`` 取 ``task`` / ``all`` / ``preset``；``agent`` 参与键，
    因此 ``/task all`` 的每个子任务、以及绑定不同 Agent 的 preset 各自独立去重。

    **机器人账号取事件自带的规范化 `message.self_id`**（OneBot 每条事件都携带，是这条
    消息的真实收件账号）；`bot_account` 只在该事件缺失身份时作为兜底，因此传输层
    "最后收到的账号"不会改写已被排队旧事件的键（F5）。
    """
    message_id = message.message_id
    if message_id is None:
        return QQSubmissionKey("", "缺少 message_id：不做去重（兼容行为）", None, target)
    event_account = int(message.self_id) if message.self_id else 0
    account = event_account if event_account > 0 else (
        bot_account if isinstance(bot_account, int) and bot_account > 0 else "-"
    )
    parts = [
        QQ_DEDUP_KEY_VERSION,
        QQ_DEDUP_ENTRY,
        str(account),
        str(int(message.group_id)),
        str(int(message.user_id)),
        str(int(message_id)),
        target,
    ]
    if preset:
        parts.append(preset)
    parts.append(agent or "-")
    return QQSubmissionKey("|".join(parts), "", message_id, target)


def duplicate_reply(existing: dict) -> str:
    """重复投递的回执：指向**既有任务**，不产生新的任务、目录选择或确认回执。"""
    task_id = str(existing.get("task_id") or "")
    status = str(existing.get("status") or "")
    if status in ("queued", "running", "awaiting_selection", "awaiting_confirmation"):
        return f"该消息已受理（task {task_id}，状态 {status}），未重复执行；用 /task status {task_id} 查看。"
    return f"该消息已处理过（task {task_id}，状态 {status}），未重复执行；用 /task result {task_id} 查看结果。"


# HTTP 入口命名空间（F2）：把客户端原始 Idempotency-Key 包成 v1|http|<raw>，
# 这样"字面等于 QQ 整串 key"的原始值也不会跨 entry 命中。
HTTP_DEDUP_ENTRY = "http"

# 幂等键方案标记（tasks.key_scheme 列）：
# - 新 HTTP 记录写 "http:v1"；
# - 升级前的旧记录保持 NULL（按"原始键"语义解释）。
# 查询按方案过滤，因此"新键 v1|http|<raw>"与"字面相同的旧原始键"各自成行、
# 互不冒名命中；既不清数据，也不改 DB 版本号，旧原始键重放仍命中旧行。
HTTP_KEY_SCHEME = "http:v1"


def http_namespaced_key(raw_key: str) -> str:
    """HTTP 新键：``v1|http|<raw>``（入口命名空间，永不与 QQ 键字面相同）。"""
    return f"{QQ_DEDUP_KEY_VERSION}|{HTTP_DEDUP_ENTRY}|{raw_key}"


def http_stored_key_variants(raw_key: str, *, limit: int = 8) -> tuple[str, ...]:
    """HTTP 新方案的存储键候选：首选 ``v1|http|<raw>``；字面量被旧记录占用时用 ``#n`` 变体。

    候选顺序由 `TaskStore.idempotency_key_variants()` 统一定义；但"命中哪一行"不能只看
    字面量——归属由 `TaskStore._row_owns_key()` 按真实逻辑键校验（`v1|http|A#2` 既可能是
    raw=``A#2`` 的基键，也可能是 raw=``A`` 的变体槽，必须由登记时记录的逻辑键区分）。
    """
    return idempotency_key_variants(http_namespaced_key(raw_key), limit=limit)
