"""任务、目录绑定、确认与文件变更的 SQLite 持久化。

- 复用 ContextStore 的同一条 sqlite3 连接（与 team 存储一致，避免第二条连接写同一 WAL）；
- 迁移只用 `CREATE TABLE IF NOT EXISTS` 与受控的 `ALTER TABLE ADD COLUMN`，可重复执行；
- 记录 schema_version；旧库缺少新列时按 text 语义解释，不删除任何历史数据；
- 服务重启不自动恢复执行：未完成任务一律标记 interrupted。
"""

from __future__ import annotations

import json
import sqlite3
import time

from app.models import new_task_id

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    entry TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    backend TEXT,
    model TEXT,
    effort TEXT,
    mode TEXT NOT NULL DEFAULT 'text',
    effort_set INTEGER NOT NULL DEFAULT 0,
    workspace TEXT,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    team_id TEXT,
    step_id TEXT,
    round INTEGER,
    idempotency_key TEXT,
    key_scheme TEXT,
    logical_key TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    result_hash TEXT,
    result_summary TEXT,
    result_text TEXT,
    operation TEXT,
    action_counts TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (group_id, user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idem
    ON tasks (group_id, user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events ON task_events (task_id, id);
CREATE TABLE IF NOT EXISTS workspace_bindings (
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS file_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    team_id TEXT,
    step_id TEXT,
    round INTEGER,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    before_hash TEXT,
    after_hash TEXT,
    backup TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_changes_task ON file_changes (task_id, id);
"""

# 任务状态：等待目录选择 / 等待确认 / 排队 / 运行 / 成功 / 完成但无变更 / 失败 / 取消 / 中断
#
# V01（第六轮）：`no_change` 表示"会话正常结束，但服务端没有记录到任何实际操作"。
# 它与 `succeeded` 一样是终态（不会被晚到结果覆盖、不阻塞同目录后续任务），
# 但**绝不**能被当作"文件已修改"：变更清单为空、操作类别为 none。
TASK_STATUSES = (
    "awaiting_selection", "awaiting_confirmation", "queued", "running",
    "succeeded", "no_change", "failed", "cancelled", "interrupted",
)
TERMINAL_STATUSES = ("succeeded", "no_change", "failed", "cancelled", "interrupted")

# find_by_idempotency 的 key_scheme 哨兵：不传 = 不过滤；None = 只查旧记录（scheme IS NULL）
_UNSET = object()


class IdempotencyKeyConflict(RuntimeError):
    """幂等键的所有存储候选都被**其它归属**的记录占用：明确拒绝本次提交。

    绝不把别人的（跨入口/方案/逻辑键）记录当作"重复提交"返回，也绝不覆盖或新增变体行。
    """


def idempotency_key_variants(key: str, *, limit: int = 8) -> tuple[str, ...]:
    """存储键候选：首选原键；被**其它归属**的字面量占用时追加 ``#n`` 变体，候选数有界。

    旧记录是升级前写入、之后不再新增的有限集合，因此候选数有界；变体只在冲突行归属
    不同（``entry``/``key_scheme``/真实逻辑键不同）时才会用到，同一入口的同一条提交仍只有一行。
    """
    return (key, *(f"{key}#{index}" for index in range(2, limit + 1)))


class TaskStore:
    """任务相关持久化。所有方法同步、参数化。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    # ---------- 迁移 ----------

    def migrate(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._ensure_columns()
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _ensure_columns(self) -> None:
        """可重复执行的列补齐：旧库缺列时按兼容语义补上（不删除任何数据）。"""
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column, ddl in (
            ("result_text", "ALTER TABLE tasks ADD COLUMN result_text TEXT"),
            ("effort_set", "ALTER TABLE tasks ADD COLUMN effort_set INTEGER NOT NULL DEFAULT 0"),
            ("result_summary", "ALTER TABLE tasks ADD COLUMN result_summary TEXT"),
            ("result_hash", "ALTER TABLE tasks ADD COLUMN result_hash TEXT"),
            # V01：操作结果类别与逐动作回执（旧库按"未记录"解释，绝不回填猜测值）
            ("operation", "ALTER TABLE tasks ADD COLUMN operation TEXT"),
            ("action_counts", "ALTER TABLE tasks ADD COLUMN action_counts TEXT"),
            # F2：幂等键方案标记。旧记录保持 NULL（= 升级前的原始键语义），
            # 新 HTTP 记录写 "http:v1"，从而与字面相同的旧原始键互不冒名命中。
            ("key_scheme", "ALTER TABLE tasks ADD COLUMN key_scheme TEXT"),
            # F2：真实逻辑键。新记录写"本次提交的逻辑键"（存储键可能是 `<key>#n` 变体），
            # 旧记录保持 NULL（按"存储键即逻辑键"的保守语义解释，不做数据回填）。
            ("logical_key", "ALTER TABLE tasks ADD COLUMN logical_key TEXT"),
        ):
            if column not in existing:
                self._conn.execute(ddl)

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    # ---------- 任务 ----------

    def new_id(self) -> str:
        return new_task_id()

    def create_task(
        self, *, entry: str, group_id: int, user_id: int, agent: str, prompt: str,
        mode: str = "text", workspace: str | None = None, backend: str | None = None,
        model: str | None = None, effort: str | None = None, effort_set: bool = False,
        status: str = "queued",
        team_id: str | None = None, step_id: str | None = None, round_no: int | None = None,
        idempotency_key: str | None = None, task_id: str | None = None,
        key_scheme: str | None = None,
    ) -> str:
        identifier = task_id or new_task_id()
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, backend, model, "
                "effort, effort_set, mode, workspace, prompt, status, team_id, step_id, round, "
                "idempotency_key, key_scheme, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identifier, entry, group_id, user_id, agent, backend, model, effort,
                 int(effort_set), mode, workspace, prompt, status, team_id, step_id, round_no,
                 idempotency_key, key_scheme, now, now),
            )
        return identifier

    def register_idempotent_task(
        self, *, entry: str, group_id: int, user_id: int, agent: str, prompt: str,
        idempotency_key: str, mode: str = "text", workspace: str | None = None,
        backend: str | None = None, model: str | None = None, effort: str | None = None,
        effort_set: bool = False, status: str = "queued", task_id: str | None = None,
        key_scheme: str | None = None, logical_key: str | None = None,
    ) -> tuple[dict | None, bool]:
        """**原子**登记一条带幂等键的任务；返回 ``(任务记录, 是否新建)``。

        - 唯一索引 ``idx_tasks_idem (group_id, user_id, idempotency_key)`` 是唯一裁决者：
          插入与冲突检测在同一条 SQL 里完成，因此顺序重放与并发重放都只会有一次插入成功，
          **不依赖**"先查再插"（存在 TOCTOU 窗口），也**不**把唯一约束异常当作"已完成去重"。
        - 冲突行必须**确属本次提交**才算同一条提交：新行按（入口、``key_scheme``、**真实
          逻辑键** ``logical_key``）三者校验；没有逻辑键记录的旧行按"基键位置 + 基键被
          其它归属占用"的结构证据校验（见 `_row_owns_key()`）。只看存储键字面/入口/方案
          会把"变体槽位恰好等于别人基键"的合法不同提交误判成重复（例如 raw=A 的变体槽与
          raw=A#2 的基键同为 ``v1|http|A#2``）。
        - 字面量被其它归属的记录占用时，改用 ``<key>#n`` 变体重新原子插入：既不误判为
          重复，也**不覆盖**别人的记录。
        - 所有候选都被其它归属占用时抛 `IdempotencyKeyConflict`（明确拒绝），**绝不**把
          别人的记录当 Duplicate 返回，也绝不覆盖或重复执行。
        - **任何已存在的任务都不覆盖**（含 ``succeeded``/``no_change``/``failed``/``cancelled``/
          ``interrupted``）：同一条已投递消息**永不自动重跑**。要重新执行请用新的消息
          （新的 ``message_id`` ⇒ 新键），从而不会把"手动重发"误当成"自动重试"。
        """
        identifier = task_id or new_task_id()
        now = time.time()
        if not idempotency_key:
            # 无幂等键：不做任何去重（调用方在缺少消息标识时走这条兼容路径）
            with self._conn:
                self._conn.execute(
                    "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, backend, model, "
                    "effort, effort_set, mode, workspace, prompt, status, idempotency_key, "
                    "key_scheme, logical_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                    (identifier, entry, group_id, user_id, agent, backend, model, effort,
                     int(effort_set), mode, workspace, prompt, status, now, now),
                )
            return self.get_task(identifier), True
        logical = logical_key if logical_key is not None else idempotency_key
        base_blocked = False
        with self._conn:
            for index, stored_key in enumerate(self._key_variants(idempotency_key)):
                cursor = self._conn.execute(
                    "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, backend, model, "
                    "effort, effort_set, mode, workspace, prompt, status, idempotency_key, "
                    "key_scheme, logical_key, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (group_id, user_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL DO NOTHING",
                    (identifier, entry, group_id, user_id, agent, backend, model, effort,
                     int(effort_set), mode, workspace, prompt, status, stored_key,
                     key_scheme, logical, now, now),
                )
                if cursor.rowcount == 1:
                    return self.get_task(identifier), True
                blocking = self.find_by_idempotency(
                    group_id=group_id, user_id=user_id, key=stored_key
                )
                if blocking is None:
                    # 唯一索引命中但按 (group,user,key) 读不到：只可能是并发写尚未可见的
                    # 极端情形。报"未新建"，由调用方按"重复投递"拒绝（宁可少执行，
                    # 不重复执行）；绝不换变体再插一行。
                    return None, False
                if self._row_owns_key(
                    blocking, entry=entry, key_scheme=key_scheme, logical_key=logical,
                    base_blocked=base_blocked or index == 0,
                ):
                    return blocking, False
                if index == 0:
                    # 基键被非本方行占用：变体槽位上的旧变体行（无逻辑键记录）自此可被认领
                    base_blocked = True
            # 所有候选都被其它归属的记录占用（需要已有大量同名字面量，实际不可达）：
            # 明确拒绝本次提交——绝不把别人的跨 entry/scheme 记录当 Duplicate 返回。
            raise IdempotencyKeyConflict(
                "该幂等键的存储候选已被其它入口/方案的既有记录占用，本次未登记也未执行；"
                "请更换 Idempotency-Key 后重试。"
            )

    @staticmethod
    def _row_owns_key(
        row: dict, *, entry: str, key_scheme: str | None, logical_key: str, base_blocked: bool,
    ) -> bool:
        """判断既有行是否**确属**本次提交（真实逻辑键归属），而不是只看存储键/entry/scheme。

        - 有 ``logical_key`` 记录的新行：入口、方案与真实逻辑键三者都相同才认领。因此
          raw=``A`` 的 ``#n`` 变体槽（逻辑键仍是 ``…|A``）不会被 raw=``A#2`` 的基键请求
          冒名认领，反之亦然。
        - 没有 ``logical_key`` 记录的旧行（升级前写入，无法回填）：
          - 位于基键位置（``base_blocked`` 无歧义，存储键恰好等于本次逻辑键）→ 按旧语义认领；
          - 位于变体槽位 → 只有能证明**基键确实被其它归属占用**（旧代码只在这种情况下
            写变体行）时才认领，保证"被旧行占键后同一请求重放/并发仍同一任务"；
            否则一律不认领（宁可明确拒绝，也不冒名认领别人的基键）。
        """
        stored_logical = row.get("logical_key")
        same_owner = (
            row.get("entry") == entry
            and (row.get("key_scheme") or None) == (key_scheme or None)
        )
        if stored_logical is not None:
            return bool(same_owner and stored_logical == logical_key)
        return bool(same_owner and base_blocked)

    @staticmethod
    def _key_variants(key: str, *, limit: int = 8) -> tuple[str, ...]:
        """存储键候选（与 `idempotency_key_variants()` 同一顺序）。"""
        return idempotency_key_variants(key, limit=limit)

    def find_owned_by_idempotency(
        self, *, entry: str, group_id: int, user_id: int, idempotency_key: str,
        key_scheme: str | None = None, logical_key: str | None = None,
    ) -> dict | None:
        """按候选存储键顺序查**确属本次提交**的既有行（入口重放查询用）。

        与 `register_idempotent_task()` 使用同一候选顺序和同一归属校验：因此"命中"与
        "登记"永远落在同一行；归属不明的变体槽位（含别人的基键）不会被返回。
        """
        logical = logical_key if logical_key is not None else idempotency_key
        base_row = self.find_by_idempotency(
            group_id=group_id, user_id=user_id, key=idempotency_key
        )
        base_blocked = base_row is not None and not self._row_owns_key(
            base_row, entry=entry, key_scheme=key_scheme, logical_key=logical, base_blocked=True,
        )
        for index, candidate in enumerate(self._key_variants(idempotency_key)):
            row = self.find_by_idempotency(
                group_id=group_id, user_id=user_id, key=candidate, entry=entry,
                key_scheme=key_scheme,
            )
            if row is not None and self._row_owns_key(
                row, entry=entry, key_scheme=key_scheme, logical_key=logical,
                base_blocked=base_blocked or index == 0,
            ):
                return row
        return None

    def get_task(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT task_id, entry, group_id, user_id, agent, backend, model, effort, "
            "effort_set, mode, workspace, prompt, status, error, team_id, step_id, round, "
            "idempotency_key, key_scheme, logical_key, created_at, updated_at, duration_ms, "
            "changed_files, result_hash, result_summary, result_text, operation, action_counts "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return self._task_row(row) if row else None

    @staticmethod
    def _task_row(row) -> dict:
        keys = (
            "task_id", "entry", "group_id", "user_id", "agent", "backend", "model", "effort",
            "effort_set", "mode", "workspace", "prompt", "status", "error", "team_id",
            "step_id", "round",
            "idempotency_key", "key_scheme", "logical_key", "created_at", "updated_at",
            "duration_ms", "changed_files", "result_hash", "result_summary", "result_text",
            "operation", "action_counts",
        )
        return dict(zip(keys, row, strict=False))

    def find_by_idempotency(
        self, *, group_id: int, user_id: int, key: str, entry: str | None = None,
        key_scheme: object = _UNSET,
    ) -> dict | None:
        """按 (group, user, key) 查既有任务；``entry`` 非空时只查该入口的记录。

        HTTP 入口必须带 ``entry="http"``：这样即使客户端传来的原始 key 与某个 QQ 任务的
        整串键字面相同，也不会跨入口命中（F2）。QQ/其它调用方不传 entry，保持原语义。

        ``key_scheme`` 区分"本方案新记录"与"升级前的旧记录"（旧记录该列为 NULL）：
        不传 = 不过滤；``None`` = 只查旧记录；字符串 = 只查该方案的记录。HTTP 新键
        ``v1|http|<raw>`` 与字面相同的旧原始键因此不会互相冒名命中。
        """
        sql = (
            "SELECT task_id, entry, group_id, user_id, agent, backend, model, effort, "
            "effort_set, mode, workspace, prompt, status, error, team_id, step_id, round, "
            "idempotency_key, key_scheme, logical_key, created_at, updated_at, duration_ms, "
            "changed_files, "
            "result_hash, result_summary, result_text, operation, action_counts FROM tasks "
            "WHERE group_id=? AND user_id=? AND idempotency_key=?"
        )
        params: list[object] = [group_id, user_id, key]
        if entry is not None:
            sql += " AND entry=?"
            params.append(entry)
        if key_scheme is not _UNSET:
            if key_scheme is None:
                sql += " AND key_scheme IS NULL"
            else:
                sql += " AND key_scheme=?"
                params.append(key_scheme)
        row = self._conn.execute(sql, params).fetchone()
        return self._task_row(row) if row else None

    def update_task(
        self, task_id: str, *, status: str | None = None, error: str | None = None,
        duration_ms: int | None = None, changed_files: int | None = None,
        result_hash: str | None = None, result_summary: str | None = None,
        result_text: str | None = None, operation: str | None = None,
        action_counts: str | None = None,
        workspace: str | None = None, model: str | None = None, effort: str | None = None,
        backend: str | None = None, mode: str | None = None,
    ) -> bool:
        sets: list[str] = ["updated_at=?"]
        values: list[object] = [time.time()]
        for column, value in (
            ("status", status), ("error", error), ("duration_ms", duration_ms),
            ("changed_files", changed_files), ("result_hash", result_hash),
            ("result_summary", result_summary),
            # L02：result_text 必须在 SET 列表里，否则成功任务的正文永远写不进库，
            # HTTP /tasks/{id}/result 只能返回空 content。
            ("result_text", result_text),
            # V01：操作类别与动作回执（QQ/HTTP/team 共用的判定结果）
            ("operation", operation), ("action_counts", action_counts),
            ("workspace", workspace),
            ("model", model), ("effort", effort), ("backend", backend), ("mode", mode),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                values.append(value)
        values.append(task_id)
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", tuple(values)
            )
        return bool(cursor.rowcount)

    def update_status_if_active(self, task_id: str, status: str, *, error: str | None = None) -> bool:
        """条件更新：只把非终态任务改到新状态，晚到事件不能覆盖终态。"""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE tasks SET status=?, error=?, updated_at=? "
                f"WHERE task_id=? AND status NOT IN ({placeholders})",
                (status, error, time.time(), task_id, *TERMINAL_STATUSES),
            )
        return bool(cursor.rowcount)

    def list_recent(self, *, group_id: int, user_id: int, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT task_id, entry, group_id, user_id, agent, backend, model, effort, "
            "effort_set, mode, workspace, prompt, status, error, team_id, step_id, round, "
            "idempotency_key, key_scheme, logical_key, created_at, updated_at, duration_ms, "
            "changed_files, "
            "result_hash, result_summary, result_text, operation, action_counts "
            "FROM tasks WHERE group_id=? AND user_id=? ORDER BY updated_at DESC LIMIT ?",
            (group_id, user_id, limit),
        ).fetchall()
        return [self._task_row(row) for row in rows]

    def mark_interrupted_on_startup(self) -> int:
        """服务启动：未完成任务标记 interrupted，绝不自动重新调用模型。"""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE tasks SET status='interrupted', updated_at=? "
                f"WHERE status NOT IN ({placeholders})",
                (time.time(), *TERMINAL_STATUSES),
            )
        return cursor.rowcount or 0

    # ---------- 事件 ----------

    def add_event(self, task_id: str, event: str, detail: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                (task_id, event, detail, time.time()),
            )

    def events(self, task_id: str, limit: int = 50) -> list[tuple[str, str | None, float]]:
        rows = self._conn.execute(
            "SELECT event, detail, created_at FROM task_events WHERE task_id=? ORDER BY id LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    # ---------- 目录绑定 ----------

    def get_binding(self, *, group_id: int, user_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT path FROM workspace_bindings WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        return row[0] if row else None

    def set_binding(self, *, group_id: int, user_id: int, path: str) -> None:
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO workspace_bindings (group_id, user_id, path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, user_id) DO UPDATE SET path=excluded.path, "
                "updated_at=excluded.updated_at",
                (group_id, user_id, path, now, now),
            )

    def clear_binding(self, *, group_id: int, user_id: int) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM workspace_bindings WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
        return bool(cursor.rowcount)

    # ---------- 文件变更 ----------

    def record_changes(
        self, *, task_id: str, changes: list[dict], team_id: str | None = None,
        step_id: str | None = None, round_no: int | None = None,
    ) -> None:
        now = time.time()
        with self._conn:
            for change in changes:
                self._conn.execute(
                    "INSERT INTO file_changes (task_id, team_id, step_id, round, path, kind, "
                    "status, before_hash, after_hash, backup, bytes, error, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, team_id, step_id, round_no, str(change.get("path", "")),
                     str(change.get("kind", "")), str(change.get("status", "")),
                     change.get("before_hash") or "", change.get("after_hash") or "",
                     change.get("backup") or "", int(change.get("bytes") or 0),
                     change.get("error") or "", now),
                )

    def list_changes(self, *, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT path, kind, status, before_hash, after_hash, backup, bytes, error "
            "FROM file_changes WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        keys = ("path", "kind", "status", "before_hash", "after_hash", "backup", "bytes", "error")
        return [dict(zip(keys, row, strict=False)) for row in rows]

    def json_dump(self, payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def decode_action_counts(self, raw: str | None) -> dict[str, int]:
        """把库里的 action_counts 文本还原为 dict；损坏/为空时返回空 dict。"""
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): int(item) for key, item in value.items()
                if isinstance(item, (int, float))}
