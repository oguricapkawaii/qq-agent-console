"""协作任务的 SQLite 持久化。

- 复用 ContextStore 的同一条 sqlite3 连接（见 `ContextStore.connection`），
  不新开第二条连接写同一个 WAL 数据库；
- 迁移只用 `CREATE TABLE IF NOT EXISTS`，不重建、不清空既有表；
- 所有方法同步、参数化，毫秒级返回；调用方保证不在 await 期间持有事务；
- 幂等与竞争保护依赖条件 UPDATE 的 rowcount（run/cancel/finish 竞争）。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from app.models import InvocationOptions
from app.workflows.models import (
    FrozenChoice,
    FrozenStep,
    PlanSnapshot,
    RunRecord,
    StepRunRecord,
    StepSpec,
    StepStatus,
    TeamStatus,
    WorkflowRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_workflows (
    team_id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    owner_user_id INTEGER NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 0,
    draft_version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_owner
    ON team_workflows (group_id, owner_user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_team_status
    ON team_workflows (status);
CREATE TABLE IF NOT EXISTS team_steps (
    team_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    agent TEXT NOT NULL,
    instruction TEXT NOT NULL,
    model TEXT,
    effort TEXT,
    effort_set INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'text',
    backend TEXT,
    workspace TEXT,
    find_keyword TEXT,
    PRIMARY KEY (team_id, step_id)
);
CREATE TABLE IF NOT EXISTS team_dependencies (
    team_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (team_id, step_id, depends_on)
);
CREATE TABLE IF NOT EXISTS team_runs (
    team_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    PRIMARY KEY (team_id, round)
);
CREATE TABLE IF NOT EXISTS team_step_runs (
    team_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    source_round INTEGER,
    task_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    answer TEXT NOT NULL DEFAULT '',
    error TEXT,
    model TEXT,
    effort TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    finished_at REAL,
    change_fingerprint TEXT,
    changed_files INTEGER NOT NULL DEFAULT 0,
    commands TEXT,
    detail TEXT,
    input_fingerprint TEXT,
    PRIMARY KEY (team_id, round, step_id)
);
CREATE TABLE IF NOT EXISTS team_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
    round INTEGER,
    step_id TEXT,
    event TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_events
    ON team_events (team_id, id);
"""


def snapshot_to_json(snapshot: PlanSnapshot) -> str:
    return json.dumps(
        {
            "team_id": snapshot.team_id,
            "goal": snapshot.goal,
            "steps": [
                {
                    "step_id": step.step_id,
                    "seq": step.seq,
                    "agent": step.agent,
                    "instruction": step.instruction,
                    "depends_on": list(step.depends_on),
                    "model": step.choice.model,
                    "effort": step.choice.effort,
                    "effort_set": step.choice.effort_set,
                    "mode": step.mode,
                    "backend": step.backend,
                    "workspace": step.workspace,
                    "grant_label": step.grant_label,
                }
                for step in snapshot.steps
            ],
        },
        ensure_ascii=False,
    )


def snapshot_from_json(raw: str) -> PlanSnapshot:
    data = json.loads(raw)
    steps = tuple(
        FrozenStep(
            step_id=item["step_id"],
            seq=int(item["seq"]),
            agent=item["agent"],
            instruction=item["instruction"],
            depends_on=tuple(item.get("depends_on") or ()),
            choice=FrozenChoice(
                model=item["model"],
                effort=item.get("effort"),
                effort_set=bool(item.get("effort_set", False)),
            ),
            # 旧冻结快照没有这些字段：按 text 解释，不授予文件权限
            mode=(item.get("mode") or "text"),
            backend=item.get("backend"),
            workspace=item.get("workspace"),
            grant_label=item.get("grant_label") or "",
        )
        for item in data.get("steps", [])
    )
    return PlanSnapshot(team_id=data["team_id"], goal=data["goal"], steps=steps)


def _workflow(row: sqlite3.Row | tuple) -> WorkflowRecord:
    return WorkflowRecord(
        team_id=row[0], group_id=row[1], owner_user_id=row[2], goal=row[3],
        status=row[4], round=row[5], created_at=row[6], updated_at=row[7],
    )


def _load_fingerprint(raw) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def _step_run(row: sqlite3.Row | tuple) -> StepRunRecord:
    commands: tuple[str, ...] = ()
    raw_commands = row[16] if len(row) > 16 else None
    if raw_commands:
        try:
            parsed = json.loads(raw_commands)
            if isinstance(parsed, list):
                commands = tuple(str(item) for item in parsed)
        except ValueError:
            commands = ()
    return StepRunRecord(
        team_id=row[0], round=row[1], step_id=row[2], status=row[3],
        answer=row[4] or "", error=row[5], source_round=row[6], task_id=row[7],
        attempts=row[8], model=row[9] or "", effort=row[10],
        duration_ms=row[11] or 0, started_at=row[12], finished_at=row[13],
        change_fingerprint=(row[14] or "") if len(row) > 14 else "",
        changed_files=(row[15] or 0) if len(row) > 15 else 0,
        commands=commands,
        detail=(row[17] or "") if len(row) > 17 else "",
        input_fingerprint=_load_fingerprint(row[18]) if len(row) > 18 else {},
    )


class WorkflowStore:
    """协作任务存储。所有方法同步执行，异常只在真正非法时抛出。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    # ---------- 迁移与审计 ----------

    def migrate(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._ensure_columns()

    def _ensure_columns(self) -> None:
        """可重复执行的列补齐；旧库缺列时按 text 语义解释，不删除任何数据。"""
        additions = {
            "team_workflows": (
                ("draft_version", "ALTER TABLE team_workflows ADD COLUMN draft_version INTEGER NOT NULL DEFAULT 0"),
            ),
            "team_steps": (
                ("mode", "ALTER TABLE team_steps ADD COLUMN mode TEXT NOT NULL DEFAULT 'text'"),
                ("backend", "ALTER TABLE team_steps ADD COLUMN backend TEXT"),
                ("workspace", "ALTER TABLE team_steps ADD COLUMN workspace TEXT"),
                ("find_keyword", "ALTER TABLE team_steps ADD COLUMN find_keyword TEXT"),
            ),
            "team_step_runs": (
                ("change_fingerprint", "ALTER TABLE team_step_runs ADD COLUMN change_fingerprint TEXT"),
                ("changed_files", "ALTER TABLE team_step_runs ADD COLUMN changed_files INTEGER NOT NULL DEFAULT 0"),
                ("commands", "ALTER TABLE team_step_runs ADD COLUMN commands TEXT"),
                ("detail", "ALTER TABLE team_step_runs ADD COLUMN detail TEXT"),
                ("input_fingerprint", "ALTER TABLE team_step_runs ADD COLUMN input_fingerprint TEXT"),
            ),
        }
        for table, columns in additions.items():
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in existing:
                    self._conn.execute(ddl)

    # ---------- 草稿版本 ----------

    def draft_version(self, team_id: str) -> int:
        row = self._conn.execute(
            "SELECT draft_version FROM team_workflows WHERE team_id=?", (team_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def add_event(self, team_id: str, event: str, *, round: int | None = None,
                  step_id: str | None = None, detail: str | None = None) -> None:
        """审计事件：只记状态变化与操作名，不保存密钥或原始接口事件流。"""
        with self._conn:
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, step_id, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (team_id, round, step_id, event, detail, time.time()),
            )

    # ---------- 草稿 ----------

    def new_team_id(self) -> str:
        return "t" + uuid.uuid4().hex[:8]

    def create_draft(self, *, group_id: int, owner_user_id: int, goal: str) -> WorkflowRecord:
        now = time.time()
        for _ in range(8):
            team_id = self.new_team_id()
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO team_workflows "
                        "(team_id, group_id, owner_user_id, goal, status, round, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                        (team_id, group_id, owner_user_id, goal, TeamStatus.DRAFT, now, now),
                    )
                    self._conn.execute(
                        "INSERT INTO team_events (team_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                        (team_id, "create", goal[:200], now),
                    )
                return self.get_workflow(team_id)  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("无法分配团队 ID")

    def get_workflow(self, team_id: str) -> WorkflowRecord | None:
        row = self._conn.execute(
            "SELECT team_id, group_id, owner_user_id, goal, status, round, created_at, updated_at "
            "FROM team_workflows WHERE team_id=?",
            (team_id,),
        ).fetchone()
        return _workflow(row) if row else None

    def list_recent_teams(self, *, group_id: int, owner_user_id: int, limit: int = 10) -> list[WorkflowRecord]:
        rows = self._conn.execute(
            "SELECT team_id, group_id, owner_user_id, goal, status, round, created_at, updated_at "
            "FROM team_workflows WHERE group_id=? AND owner_user_id=? "
            "ORDER BY updated_at DESC, team_id DESC LIMIT ?",
            (group_id, owner_user_id, max(1, limit)),
        ).fetchall()
        return [_workflow(row) for row in rows]

    def count_active_global(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM team_workflows WHERE status IN (?, ?)",
            (TeamStatus.QUEUED, TeamStatus.RUNNING),
        ).fetchone()
        return int(row[0])

    def count_active_user(self, owner_user_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM team_workflows WHERE owner_user_id=? AND status IN (?, ?)",
            (owner_user_id, TeamStatus.QUEUED, TeamStatus.RUNNING),
        ).fetchone()
        return int(row[0])

    def preferences(self, *, group_id: int, user_id: int, agent: str) -> InvocationOptions | None:
        """只读复用既有 agent_preferences（/set 的个人选择），不写入。"""
        row = self._conn.execute(
            "SELECT model, effort FROM agent_preferences WHERE group_id=? AND user_id=? AND agent=?",
            (group_id, user_id, agent),
        ).fetchone()
        return InvocationOptions(model=row[0], effort=row[1]) if row else None

    # ---------- 步骤定义 ----------

    def list_steps(self, team_id: str) -> list[StepSpec]:
        rows = self._conn.execute(
            "SELECT step_id, seq, agent, instruction, model, effort, effort_set, "
            "mode, backend, workspace, find_keyword "
            "FROM team_steps WHERE team_id=? ORDER BY seq",
            (team_id,),
        ).fetchall()
        deps = self.dependencies(team_id)
        return [
            StepSpec(
                step_id=row[0], seq=row[1], agent=row[2], instruction=row[3],
                depends_on=tuple(sorted(deps.get(row[0], set()))),
                model=row[4], effort=row[5], effort_set=bool(row[6]),
                mode=(row[7] or "text"), backend=row[8], workspace=row[9],
                find_keyword=row[10],
            )
            for row in rows
        ]

    def dependencies(self, team_id: str) -> dict[str, set[str]]:
        rows = self._conn.execute(
            "SELECT step_id, depends_on FROM team_dependencies WHERE team_id=?",
            (team_id,),
        ).fetchall()
        result: dict[str, set[str]] = {}
        for step_id, depends_on in rows:
            result.setdefault(step_id, set()).add(depends_on)
        return result

    def has_step(self, team_id: str, step_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM team_steps WHERE team_id=? AND step_id=?",
            (team_id, step_id),
        ).fetchone()
        return row is not None

    def count_steps(self, team_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM team_steps WHERE team_id=?", (team_id,)
        ).fetchone()
        return int(row[0])

    def replace_step(
        self, *, team_id: str, step_id: str, agent: str, instruction: str,
        depends_on: tuple[str, ...], model: str | None, effort: str | None,
        effort_set: bool, mode: str = "text", backend: str | None = None,
        workspace: str | None = None, find_keyword: str | None = None,
    ) -> bool:
        """草稿中新增或完整替换一步；返回是否为新建。"""
        now = time.time()
        with self._conn:
            row = self._conn.execute(
                "SELECT status FROM team_workflows WHERE team_id=?", (team_id,)
            ).fetchone()
            if row is None:
                raise LookupError("not_found")
            if row[0] != TeamStatus.DRAFT:
                raise RuntimeError("not_draft")
            existing = self._conn.execute(
                "SELECT seq FROM team_steps WHERE team_id=? AND step_id=?",
                (team_id, step_id),
            ).fetchone()
            if existing is None:
                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 FROM team_steps WHERE team_id=?",
                    (team_id,),
                ).fetchone()
                seq = int(seq_row[0])
                self._conn.execute(
                    "INSERT INTO team_steps "
                    "(team_id, step_id, seq, agent, instruction, model, effort, effort_set, "
                    "mode, backend, workspace, find_keyword) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (team_id, step_id, seq, agent, instruction, model, effort, int(effort_set),
                     mode, backend, workspace, find_keyword),
                )
                created = True
            else:
                seq = int(existing[0])
                self._conn.execute(
                    "UPDATE team_steps SET agent=?, instruction=?, model=?, effort=?, "
                    "effort_set=?, mode=?, backend=?, workspace=?, find_keyword=? "
                    "WHERE team_id=? AND step_id=?",
                    (agent, instruction, model, effort, int(effort_set), mode, backend,
                     workspace, find_keyword, team_id, step_id),
                )
                self._conn.execute(
                    "DELETE FROM team_dependencies WHERE team_id=? AND step_id=?",
                    (team_id, step_id),
                )
                created = False
            for dep in depends_on:
                self._conn.execute(
                    "INSERT OR IGNORE INTO team_dependencies (team_id, step_id, depends_on) "
                    "VALUES (?, ?, ?)",
                    (team_id, step_id, dep),
                )
            self._conn.execute(
                "INSERT INTO team_events (team_id, step_id, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, step_id, "step_create" if created else "step_replace",
                 f"agent={agent} seq={seq}", now),
            )
            self._conn.execute(
                "UPDATE team_workflows SET updated_at=?, draft_version=draft_version+1 "
                "WHERE team_id=?",
                (now, team_id),
            )
        return created

    def set_step_workspace(
        self, *, team_id: str, step_id: str, workspace: str, mode: str | None = None,
    ) -> bool:
        """目录选定后写入草稿步骤（同时清除待选关键词）。

        只允许草稿：非草稿状态一律拒绝，避免目录确认在运行/结束的团队上生效。
        """
        now = time.time()
        with self._conn:
            row = self._conn.execute(
                "SELECT status FROM team_workflows WHERE team_id=?", (team_id,)
            ).fetchone()
            if row is None:
                raise LookupError("not_found")
            if row[0] != TeamStatus.DRAFT:
                raise RuntimeError("not_draft")
            cursor = self._conn.execute(
                "UPDATE team_steps SET workspace=?, find_keyword=NULL, mode=COALESCE(?, mode) "
                "WHERE team_id=? AND step_id=?",
                (workspace, mode, team_id, step_id),
            )
            self._conn.execute(
                "UPDATE team_workflows SET updated_at=?, draft_version=draft_version+1 "
                "WHERE team_id=?",
                (now, team_id),
            )
            self._conn.execute(
                "INSERT INTO team_events (team_id, step_id, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, step_id, "step_workspace", workspace, now),
            )
        return bool(cursor.rowcount)

    def remove_step(self, team_id: str, step_id: str) -> None:
        """删除草稿步骤；仍有其他步骤依赖它时拒绝。"""
        now = time.time()
        with self._conn:
            row = self._conn.execute(
                "SELECT status FROM team_workflows WHERE team_id=?", (team_id,)
            ).fetchone()
            if row is None:
                raise LookupError("not_found")
            if row[0] != TeamStatus.DRAFT:
                raise RuntimeError("not_draft")
            if not self.has_step(team_id, step_id):
                raise LookupError("step_not_found")
            dependents = [
                item[0] for item in self._conn.execute(
                    "SELECT step_id FROM team_dependencies WHERE team_id=? AND depends_on=? "
                    "ORDER BY step_id",
                    (team_id, step_id),
                ).fetchall()
            ]
            if dependents:
                raise ValueError("dependents:" + ",".join(dependents))
            self._conn.execute(
                "DELETE FROM team_dependencies WHERE team_id=? AND step_id=?",
                (team_id, step_id),
            )
            self._conn.execute(
                "DELETE FROM team_steps WHERE team_id=? AND step_id=?", (team_id, step_id)
            )
            self._conn.execute(
                "INSERT INTO team_events (team_id, step_id, event, created_at) VALUES (?, ?, ?, ?)",
                (team_id, step_id, "step_remove", now),
            )
            self._conn.execute(
                "UPDATE team_workflows SET updated_at=?, draft_version=draft_version+1 "
                "WHERE team_id=?",
                (now, team_id),
            )

    # ---------- 轮次 ----------

    def freeze_run(self, team_id: str, snapshot: PlanSnapshot) -> int | None:
        """事务内比较 draft 并冻结计划；重复 run 返回 None（不新建轮次）。"""
        now = time.time()
        plan_json = snapshot_to_json(snapshot)
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_workflows SET status=?, round=round+1, updated_at=? "
                "WHERE team_id=? AND status=?",
                (TeamStatus.QUEUED, now, team_id, TeamStatus.DRAFT),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT round FROM team_workflows WHERE team_id=?", (team_id,)
            ).fetchone()
            round_no = int(row[0])
            self._conn.execute(
                "INSERT INTO team_runs (team_id, round, status, plan_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, TeamStatus.QUEUED, plan_json, now),
            )
            for step in snapshot.steps:
                self._conn.execute(
                    "INSERT INTO team_step_runs (team_id, round, step_id, status) "
                    "VALUES (?, ?, ?, ?)",
                    (team_id, round_no, step.step_id, StepStatus.PENDING),
                )
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, "run", f"steps={len(snapshot.steps)}", now),
            )
        return round_no

    def start_retry(self, team_id: str, snapshot: PlanSnapshot) -> int | None:
        """仅允许 failed/interrupted；成功后开启新轮次并复用成功结果。"""
        now = time.time()
        plan_json = snapshot_to_json(snapshot)
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_workflows SET status=?, round=round+1, updated_at=? "
                "WHERE team_id=? AND status IN (?, ?)",
                (TeamStatus.QUEUED, now, team_id, TeamStatus.FAILED, TeamStatus.INTERRUPTED),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT round FROM team_workflows WHERE team_id=?", (team_id,)
            ).fetchone()
            round_no = int(row[0])
            prev_round = round_no - 1
            self._conn.execute(
                "INSERT INTO team_runs (team_id, round, status, plan_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, TeamStatus.QUEUED, plan_json, now),
            )
            for step in snapshot.steps:
                previous = self._conn.execute(
                    "SELECT status, source_round, task_id, attempts, answer, error, model, effort, "
                    "duration_ms, change_fingerprint, changed_files, commands, detail, "
                    "input_fingerprint "
                    "FROM team_step_runs WHERE team_id=? AND round=? AND step_id=?",
                    (team_id, prev_round, step.step_id),
                ).fetchone()
                if previous is not None and previous[0] == StepStatus.SUCCEEDED:
                    source = previous[1] if previous[1] is not None else prev_round
                    self._conn.execute(
                        "INSERT INTO team_step_runs "
                        "(team_id, round, step_id, status, source_round, task_id, attempts, answer, "
                        "error, model, effort, duration_ms, started_at, finished_at, "
                        "change_fingerprint, changed_files, commands, detail, "
                        "input_fingerprint) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (team_id, round_no, step.step_id, StepStatus.SUCCEEDED, source,
                         previous[2], previous[3], previous[4], previous[5], previous[6],
                         previous[7], previous[8], now, now, previous[9], previous[10],
                         previous[11], previous[12], previous[13]),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO team_step_runs (team_id, round, step_id, status) "
                        "VALUES (?, ?, ?, ?)",
                        (team_id, round_no, step.step_id, StepStatus.PENDING),
                    )
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, "retry", f"from_round={prev_round}", now),
            )
        return round_no

    def get_run(self, team_id: str, round_no: int) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT team_id, round, status, plan_json, created_at, started_at, finished_at, error "
            "FROM team_runs WHERE team_id=? AND round=?",
            (team_id, round_no),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            team_id=row[0], round=row[1], status=row[2], plan_json=row[3],
            created_at=row[4], started_at=row[5], finished_at=row[6], error=row[7],
        )

    def set_run_running(self, team_id: str, round_no: int) -> bool:
        now = time.time()
        with self._conn:
            run_cursor = self._conn.execute(
                "UPDATE team_runs SET status=?, started_at=? WHERE team_id=? AND round=? AND status=?",
                (TeamStatus.RUNNING, now, team_id, round_no, TeamStatus.QUEUED),
            )
            team_cursor = self._conn.execute(
                "UPDATE team_workflows SET status=?, updated_at=? WHERE team_id=? AND status=?",
                (TeamStatus.RUNNING, now, team_id, TeamStatus.QUEUED),
            )
            return run_cursor.rowcount == 1 and team_cursor.rowcount == 1

    # ---------- 步骤轮次 ----------

    def get_step_run(self, team_id: str, round_no: int, step_id: str) -> StepRunRecord | None:
        row = self._conn.execute(
            "SELECT team_id, round, step_id, status, answer, error, source_round, task_id, "
            "attempts, model, effort, duration_ms, started_at, finished_at, "
            "change_fingerprint, changed_files, commands, detail, input_fingerprint "
            "FROM team_step_runs WHERE team_id=? AND round=? AND step_id=?",
            (team_id, round_no, step_id),
        ).fetchone()
        return _step_run(row) if row else None

    def list_step_runs(self, team_id: str, round_no: int) -> dict[str, StepRunRecord]:
        rows = self._conn.execute(
            "SELECT team_id, round, step_id, status, answer, error, source_round, task_id, "
            "attempts, model, effort, duration_ms, started_at, finished_at, "
            "change_fingerprint, changed_files, commands, detail, input_fingerprint "
            "FROM team_step_runs WHERE team_id=? AND round=?",
            (team_id, round_no),
        ).fetchall()
        return {row[2]: _step_run(row) for row in rows}

    def claim_step(self, team_id: str, round_no: int, step_id: str) -> bool:
        """pending/ready -> running；晚到或重复调度不会二次执行。"""
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_step_runs SET status=?, attempts=attempts+1, started_at=? "
                "WHERE team_id=? AND round=? AND step_id=? AND status IN (?, ?)",
                (StepStatus.RUNNING, now, team_id, round_no, step_id,
                 StepStatus.PENDING, StepStatus.READY),
            )
        return cursor.rowcount == 1

    def finish_step(
        self, *, team_id: str, round_no: int, step_id: str, status: str,
        answer: str = "", error: str | None = None, duration_ms: int = 0,
        task_id: str | None = None, model: str = "", effort: str | None = None,
        change_fingerprint: str = "", changed_files: int = 0,
        commands: tuple[str, ...] = (), detail: str = "",
        input_fingerprint: dict[str, str] | None = None,
    ) -> bool:
        """running -> 终态；已取消/已超时的步骤不会被晚到结果覆盖。"""
        if status not in StepStatus.TERMINAL:
            raise ValueError(f"非法步骤终态：{status}")
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_step_runs SET status=?, answer=?, error=?, duration_ms=?, "
                "task_id=?, model=?, effort=?, finished_at=?, change_fingerprint=?, "
                "changed_files=?, commands=?, detail=?, input_fingerprint=? "
                "WHERE team_id=? AND round=? AND step_id=? AND status=?",
                (status, answer, error, duration_ms, task_id, model, effort, now,
                 change_fingerprint, changed_files,
                 json.dumps(list(commands), ensure_ascii=False) if commands else None,
                 detail or None,
                 json.dumps(input_fingerprint, ensure_ascii=False) if input_fingerprint else None,
                 team_id, round_no, step_id, StepStatus.RUNNING),
            )
        return cursor.rowcount == 1

    def mark_step_blocked(self, team_id: str, round_no: int, step_id: str, reason: str) -> bool:
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_step_runs SET status=?, error=?, finished_at=? "
                "WHERE team_id=? AND round=? AND step_id=? AND status IN (?, ?)",
                (StepStatus.BLOCKED, reason, now, team_id, round_no, step_id,
                 StepStatus.PENDING, StepStatus.READY),
            )
        return cursor.rowcount == 1

    def cancel_round(self, team_id: str, round_no: int, *, reason: str = "cancelled") -> int:
        """把本轮未完成步骤与轮次、团队置为取消；已完成结果保留。"""
        now = time.time()
        with self._conn:
            steps = self._conn.execute(
                "UPDATE team_step_runs SET status=?, error=?, finished_at=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?, ?)",
                (StepStatus.CANCELLED, reason, now, team_id, round_no,
                 StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING),
            ).rowcount
            self._conn.execute(
                "UPDATE team_runs SET status=?, finished_at=?, error=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?)",
                (TeamStatus.CANCELLED, now, reason, team_id, round_no,
                 TeamStatus.QUEUED, TeamStatus.RUNNING),
            )
            self._conn.execute(
                "UPDATE team_workflows SET status=?, updated_at=? "
                "WHERE team_id=? AND status IN (?, ?)",
                (TeamStatus.CANCELLED, now, team_id, TeamStatus.QUEUED, TeamStatus.RUNNING),
            )
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, "cancel", reason, now),
            )
        return steps or 0

    def cancel_draft(self, team_id: str) -> bool:
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_workflows SET status=?, updated_at=? WHERE team_id=? AND status=?",
                (TeamStatus.CANCELLED, now, team_id, TeamStatus.DRAFT),
            )
            if cursor.rowcount == 1:
                self._conn.execute(
                    "INSERT INTO team_events (team_id, event, created_at) VALUES (?, ?, ?)",
                    (team_id, "cancel_draft", now),
                )
        return cursor.rowcount == 1

    def interrupt_round(self, team_id: str, round_no: int, *, reason: str = "shutdown") -> int:
        """优雅关闭时把未完成步骤与轮次、团队标记 interrupted（可 retry、不自动重跑）。

        与 cancel_round 的区别：取消是用户显式指令（终态 cancelled，不可 retry），
        关闭/重启只是本机中止，用户之后可以显式 retry 恢复。
        """
        now = time.time()
        with self._conn:
            steps = self._conn.execute(
                "UPDATE team_step_runs SET status=?, error=?, finished_at=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?, ?)",
                (StepStatus.INTERRUPTED, reason, now, team_id, round_no,
                 StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING),
            ).rowcount
            self._conn.execute(
                "UPDATE team_runs SET status=?, finished_at=?, error=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?)",
                (TeamStatus.INTERRUPTED, now, reason, team_id, round_no,
                 TeamStatus.QUEUED, TeamStatus.RUNNING),
            )
            self._conn.execute(
                "UPDATE team_workflows SET status=?, updated_at=? "
                "WHERE team_id=? AND status IN (?, ?)",
                (TeamStatus.INTERRUPTED, now, team_id, TeamStatus.QUEUED, TeamStatus.RUNNING),
            )
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, "interrupted", reason, now),
            )
        return steps or 0

    def list_active_teams(self, *, group_id: int, owner_user_id: int) -> list[WorkflowRecord]:
        rows = self._conn.execute(
            "SELECT team_id, group_id, owner_user_id, goal, status, round, created_at, updated_at "
            "FROM team_workflows WHERE group_id=? AND owner_user_id=? AND status IN (?, ?) "
            "ORDER BY updated_at",
            (group_id, owner_user_id, TeamStatus.QUEUED, TeamStatus.RUNNING),
        ).fetchall()
        return [_workflow(row) for row in rows]

    def complete_round(self, team_id: str, round_no: int) -> str:
        """依据本轮步骤状态判定团队终态：全部成功才 succeeded。

        条件 UPDATE 保证已取消/已中断的轮次不会被晚到的完成信号翻转。
        """
        now = time.time()
        with self._conn:
            rows = self._conn.execute(
                "SELECT status FROM team_step_runs WHERE team_id=? AND round=?",
                (team_id, round_no),
            ).fetchall()
            statuses = [row[0] for row in rows]
            if statuses and all(status == StepStatus.SUCCEEDED for status in statuses):
                final = TeamStatus.SUCCEEDED
            else:
                final = TeamStatus.FAILED
            cursor = self._conn.execute(
                "UPDATE team_workflows SET status=?, updated_at=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?)",
                (final, now, team_id, round_no, TeamStatus.QUEUED, TeamStatus.RUNNING),
            )
            if cursor.rowcount != 1:
                row = self._conn.execute(
                    "SELECT status FROM team_workflows WHERE team_id=?", (team_id,)
                ).fetchone()
                return row[0] if row else final
            self._conn.execute(
                "UPDATE team_runs SET status=?, finished_at=? WHERE team_id=? AND round=?",
                (final, now, team_id, round_no),
            )
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, "finish", final, now),
            )
        return final

    def fail_round(self, team_id: str, round_no: int, error: str) -> bool:
        """整轮失败（如总超时/内部异常）：未完成步骤记 failed，团队记 failed。

        已是终态的轮次（cancelled/interrupted/succeeded）不会被覆盖。
        """
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE team_workflows SET status=?, updated_at=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?)",
                (TeamStatus.FAILED, now, team_id, round_no, TeamStatus.QUEUED, TeamStatus.RUNNING),
            )
            if cursor.rowcount != 1:
                return False
            self._conn.execute(
                "UPDATE team_step_runs SET status=?, error=?, finished_at=? "
                "WHERE team_id=? AND round=? AND status IN (?, ?, ?)",
                (StepStatus.FAILED, error, now, team_id, round_no,
                 StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING),
            )
            self._conn.execute(
                "UPDATE team_runs SET status=?, finished_at=?, error=? WHERE team_id=? AND round=?",
                (TeamStatus.FAILED, now, error, team_id, round_no),
            )
            self._conn.execute(
                "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (team_id, round_no, "fail", error, now),
            )
        return True

    # ---------- 查询 ----------

    def step_statuses(self, team_id: str, round_no: int) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT step_id, status FROM team_step_runs WHERE team_id=? AND round=?",
            (team_id, round_no),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def results_in_order(self, team_id: str, round_no: int) -> list[tuple[StepSpec, StepRunRecord | None]]:
        steps = self.list_steps(team_id)
        runs = self.list_step_runs(team_id, round_no)
        return [(step, runs.get(step.step_id)) for step in steps]

    def mark_interrupted_on_startup(self) -> int:
        """重启恢复：把 queued/running 的团队与未完成步骤标记 interrupted。

        不自动重新调用模型，避免重复计费或副作用；用户显式 /team retry 才恢复。
        """
        now = time.time()
        with self._conn:
            rows = self._conn.execute(
                "SELECT team_id, round FROM team_workflows WHERE status IN (?, ?)",
                (TeamStatus.QUEUED, TeamStatus.RUNNING),
            ).fetchall()
            for team_id, round_no in rows:
                self._conn.execute(
                    "UPDATE team_step_runs SET status=?, error=?, finished_at=? "
                    "WHERE team_id=? AND round=? AND status IN (?, ?, ?)",
                    (StepStatus.INTERRUPTED, "interrupted", now, team_id, round_no,
                     StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING),
                )
                self._conn.execute(
                    "UPDATE team_runs SET status=?, finished_at=?, error=? "
                    "WHERE team_id=? AND round=? AND status IN (?, ?)",
                    (TeamStatus.INTERRUPTED, now, "interrupted", team_id, round_no,
                     TeamStatus.QUEUED, TeamStatus.RUNNING),
                )
                self._conn.execute(
                    "UPDATE team_workflows SET status=?, updated_at=? WHERE team_id=? AND status IN (?, ?)",
                    (TeamStatus.INTERRUPTED, now, team_id, TeamStatus.QUEUED, TeamStatus.RUNNING),
                )
                self._conn.execute(
                    "INSERT INTO team_events (team_id, round, event, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (team_id, round_no, "interrupted", "startup", now),
                )
        return len(rows)
