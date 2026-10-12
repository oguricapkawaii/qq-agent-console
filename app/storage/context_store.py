"""基于 SQLite 的对话上下文存储。

按「群 ID + 用户 ID + Agent 名称」隔离上下文；一个 Agent 的系统提示词与
历史不会出现在另一个 Agent 的上下文中（键中包含 Agent 名，天然隔离）。

说明：操作为毫秒级的本地 SQLite 调用，MVP 直接在事件循环中同步执行。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.models import ChatMessage, InvocationOptions

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_whitelist (
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    updated_by INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS agent_preferences (
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT,
    backend TEXT,
    PRIMARY KEY (group_id, user_id, agent)
);
CREATE TABLE IF NOT EXISTS context_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctx_key
    ON context_messages (group_id, user_id, agent, id);
"""

_VALID_ROLES = ("user", "assistant")

# 共享线程在 agent 维度上的保留键：同一「群 × 用户」下所有 Agent 共用一条历史。
# 真实 Agent 名不含下划线，不会与之冲突。
SHARED_THREAD = "__shared__"

# 发言人前缀：共享线程里每条历史都标出「谁说的」，多 Agent 时才能分辨归属。
ASKER_PREFIX = "[群成员]"
AGENT_PREFIX = "[Agent]"

def thread_key(agent: str, *, shared: bool) -> str:
    """共享线程开启时，所有 Agent 落在同一个上下文键上（Agent 间互相可见）。"""
    return SHARED_THREAD if shared else agent


def speaker_prefix(display_name: str) -> str:
    return f"{AGENT_PREFIX}[{display_name}]"



class ContextStore:
    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """可重复执行的列补齐（旧库缺 backend 列时补上，不删除任何历史）。"""
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(agent_preferences)")
        }
        if "backend" not in existing:
            self._conn.execute("ALTER TABLE agent_preferences ADD COLUMN backend TEXT")

    def append(
        self, *, group_id: int, user_id: int, agent: str, role: str, content: str
    ) -> None:
        if role not in _VALID_ROLES:
            raise ValueError(f"role 必须是 {_VALID_ROLES}，收到：{role}")
        self._conn.execute(
            "INSERT INTO context_messages (group_id, user_id, agent, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, user_id, agent, role, content, time.time()),
        )
        self._conn.commit()

    def recent(
        self, *, group_id: int, user_id: int, agent: str, max_turns: int
    ) -> list[ChatMessage]:
        """返回该键下最近 max_turns 轮（user+assistant 各一条）的历史，按时间升序。"""
        limit = max(0, max_turns) * 2
        if limit == 0:
            return []
        rows = self._conn.execute(
            "SELECT role, content FROM context_messages "
            "WHERE group_id = ? AND user_id = ? AND agent = ? "
            "ORDER BY id DESC LIMIT ?",
            (group_id, user_id, agent, limit),
        ).fetchall()
        return [ChatMessage(role=role, content=content) for role, content in reversed(rows)]

    def trim(self, *, group_id: int, user_id: int, agent: str, max_turns: int) -> int:
        """只保留最近 max_turns 轮，删除更早的记录。返回删除条数。"""
        keep = max(0, max_turns) * 2
        cursor = self._conn.execute(
            "DELETE FROM context_messages "
            "WHERE group_id = ? AND user_id = ? AND agent = ? AND id NOT IN ("
            "  SELECT id FROM context_messages "
            "  WHERE group_id = ? AND user_id = ? AND agent = ? "
            "  ORDER BY id DESC LIMIT ?"
            ")",
            (group_id, user_id, agent, group_id, user_id, agent, keep),
        )
        self._conn.commit()
        return cursor.rowcount or 0

    def clear(self, *, group_id: int, user_id: int, agent: str) -> int:
        """清除该键全部历史。返回删除条数。"""
        cursor = self._conn.execute(
            "DELETE FROM context_messages "
            "WHERE group_id = ? AND user_id = ? AND agent = ?",
            (group_id, user_id, agent),
        )
        self._conn.commit()
        return cursor.rowcount or 0

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """共享的 SQLite 连接（只读访问器）。

        WorkflowStore 复用同一条连接建表与读写，避免第二条连接写同一 WAL
        数据库引入 database is locked；所有调用保持同步、参数化、毫秒级返回。
        """
        return self._conn

    def preferences(self, *, group_id: int, user_id: int, agent: str) -> InvocationOptions | None:
        row = self._conn.execute(
            "SELECT model, effort, backend FROM agent_preferences "
            "WHERE group_id=? AND user_id=? AND agent=?",
            (group_id, user_id, agent),
        ).fetchone()
        # 显式按列取值：给 agent_preferences 增列也不会静默错位。
        return InvocationOptions(model=row[0], effort=row[1], backend=row[2]) if row else None

    def set_preferences(self, *, group_id: int, user_id: int, agent: str,
                        options: InvocationOptions) -> None:
        self._conn.execute(
            "INSERT INTO agent_preferences (group_id, user_id, agent, model, effort, backend) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id,user_id,agent) DO UPDATE SET model=excluded.model, "
            "effort=excluded.effort, backend=excluded.backend",
            (group_id, user_id, agent, options.model, options.effort, options.backend),
        )
        self._conn.commit()

    def whitelist(self, group_id: int) -> dict[int, bool]:
        return {user: bool(allowed) for user, allowed in self._conn.execute(
            "SELECT user_id, allowed FROM user_whitelist WHERE group_id=?", (group_id,))}

    def set_whitelist(self, group_id: int, user_id: int, allowed: bool, actor: int) -> None:
        self._conn.execute(
            "INSERT INTO user_whitelist VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id,user_id) DO UPDATE SET allowed=excluded.allowed, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (group_id, user_id, int(allowed), actor, time.time()))
        self._conn.commit()
