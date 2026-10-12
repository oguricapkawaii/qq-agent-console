"""A20/A21：存储迁移、重启语义、新旧配置迁移与备份一致性。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.security.authz import Authorizer
from app.tasks.store import TaskStore
from app.workspaces.configurator import build_workspace_config, project_root
from app.workflows.store import WorkflowStore, snapshot_from_json, snapshot_to_json
from app.workflows.models import FrozenChoice, FrozenStep, PlanSnapshot


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        qq_allowed_user_ids=[100],
        qq_allowed_group_ids=[200],
        database_path=str(tmp_path / "t.db"),
        thinking_archive_dir="",
    )
    values.update(overrides)
    return Settings(**values)


# ---------- A21 旧目录配置迁移 ----------


def test_legacy_read_root_stays_admin_only(tmp_path) -> None:
    """F09：旧 /read 是管理员命令，迁移不得给普通成员新增 read 能力。"""
    read_root = tmp_path / "read"
    read_root.mkdir()
    config = build_workspace_config(_settings(tmp_path, file_read_root=str(read_root)))
    assert len(config.grants) == 1
    grant = config.grants[0]
    assert grant.modes == frozenset(), "旧管理员只读根不能自动扩大为成员可读"
    assert grant.admin_modes == frozenset({"read"})
    assert "edit" not in grant.admin_modes
    assert any("FILE_READ_ROOT" in line for line in config.notes)


def test_legacy_write_root_does_not_upgrade_members(tmp_path) -> None:
    write_root = tmp_path / "write"
    write_root.mkdir()
    config = build_workspace_config(_settings(tmp_path, file_write_root=str(write_root)))
    grant = config.grants[0]
    assert grant.modes == frozenset()
    assert grant.admin_modes == frozenset({"read", "edit"})
    assert any("FILE_WRITE_ROOT" in line for line in config.notes)


def test_legacy_codex_roots_become_admin_exec(tmp_path) -> None:
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    config = build_workspace_config(
        _settings(tmp_path, codex_allowed_roots=[str(codex_root)])
    )
    grant = config.grants[0]
    assert grant.modes == frozenset()
    assert grant.admin_modes == frozenset({"read", "edit", "exec"})


def test_legacy_zcode_root_bounded(tmp_path) -> None:
    zcode_root = tmp_path / "zcode"
    zcode_root.mkdir()
    config = build_workspace_config(
        _settings(tmp_path, zcode_workspace_root=str(zcode_root))
    )
    grant = config.grants[0]
    assert "exec" not in grant.admin_modes
    assert grant.modes == frozenset()


def test_new_roots_win_and_conflicts_are_reported(tmp_path) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    new_root = tmp_path / "new"
    new_root.mkdir()
    settings = _settings(
        tmp_path,
        file_read_root=str(legacy_root),
        console_workspaces={
            "roots": [{"path": str(new_root), "modes": ["read", "edit"],
                       "admin_modes": ["read", "edit"]}]
        },
    )
    config = build_workspace_config(settings)
    assert [grant.path for grant in config.grants] == [new_root.resolve()]
    assert any("忽略" in line for line in config.notes)
    assert any("FILE_READ_ROOT" in line for line in config.notes)


def test_workspace_config_defaults_and_protection(tmp_path) -> None:
    config = build_workspace_config(_settings(tmp_path))
    assert config.grants == ()
    assert project_root().resolve() in {path.resolve() for path in config.protected}
    assert (project_root() / ".env").resolve() in {
        path.resolve() for path in config.protected
    }
    assert config.exec_enabled and config.edit_enabled
    assert config.max_workers == 1


def test_workspace_config_rejects_bad_roots(tmp_path) -> None:
    from app.workspaces.configurator import WorkspaceConfigError

    missing = tmp_path / "not-there"
    with pytest.raises(WorkspaceConfigError):
        build_workspace_config(
            _settings(tmp_path, console_workspaces={"roots": [{"path": str(missing)}]})
        )
    with pytest.raises(WorkspaceConfigError):
        build_workspace_config(
            _settings(tmp_path, console_workspaces={"roots": [{"modes": ["read"]}]})
        )
    with pytest.raises(WorkspaceConfigError):
        build_workspace_config(
            _settings(tmp_path, console_workspaces={
                "roots": [{"path": str(tmp_path), "modes": ["bogus"]}]
            })
        )


def test_multi_worker_without_cross_process_lock_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        build_workspace_config(
            _settings(tmp_path, console_workspaces={"roots": [], "max_workers": 3})
        )


def test_env_file_not_modified_by_settings_loading(tmp_path, monkeypatch) -> None:
    """合成 .env：Settings 读取其中已知值，但绝不改写该文件。"""
    monkeypatch.delenv("QQ_ALLOWED_USER_IDS", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("QQ_ALLOWED_USER_IDS=101,202\n", encoding="utf-8")
    before = hashlib.sha256(env_path.read_bytes()).hexdigest()
    settings = Settings(_env_file=env_path)  # 只构造配置对象
    after = hashlib.sha256(env_path.read_bytes()).hexdigest()
    assert before == after
    assert settings.qq_allowed_user_ids == [101, 202]


# ---------- A20 存储迁移与重启 ----------


def test_task_store_migration_is_idempotent(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "t.db")
    store = TaskStore(connection)
    store.migrate()
    version = store.schema_version()
    store.migrate()
    assert store.schema_version() == version >= 2
    connection.close()


def test_old_team_snapshot_without_mode_is_text() -> None:
    legacy = json.dumps({
        "team_id": "t1", "goal": "旧计划",
        "steps": [{
            "step_id": "s1", "seq": 0, "agent": "deepseek", "instruction": "做",
            "depends_on": [], "model": "m", "effort": None, "effort_set": False,
        }],
    })
    snapshot = snapshot_from_json(legacy)
    step = snapshot.steps[0]
    assert step.mode == "text"
    assert step.workspace is None
    assert step.backend is None
    assert step.needs_workspace is False


def test_snapshot_json_roundtrip_keeps_capabilities() -> None:
    snapshot = PlanSnapshot(
        team_id="t1", goal="g",
        steps=(FrozenStep(
            step_id="s1", seq=0, agent="deepseek", instruction="i", depends_on=(),
            choice=FrozenChoice(model="m", effort="high", effort_set=True),
            mode="edit", backend="b", workspace="/tmp/x", grant_label="root",
        ),),
    )
    restored = snapshot_from_json(snapshot_to_json(snapshot))
    step = restored.steps[0]
    assert (step.mode, step.backend, step.workspace, step.grant_label) == (
        "edit", "b", "/tmp/x", "root"
    )


def test_workflow_store_migrates_old_schema(tmp_path) -> None:
    """旧库（没有 mode/backend/workspace 列）也能迁移且不丢数据。"""
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE team_workflows (
            team_id TEXT PRIMARY KEY, group_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
            round INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE team_steps (
            team_id TEXT NOT NULL, step_id TEXT NOT NULL, seq INTEGER NOT NULL,
            agent TEXT NOT NULL, instruction TEXT NOT NULL, model TEXT, effort TEXT,
            effort_set INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (team_id, step_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO team_workflows VALUES ('t1', 200, 100, '旧目标', 'draft', 0, 1, 1)"
    )
    connection.execute(
        "INSERT INTO team_steps VALUES ('t1', 's1', 0, 'deepseek', '旧指令', NULL, NULL, 0)"
    )
    connection.commit()
    store = WorkflowStore(connection)
    store.migrate()
    steps = store.list_steps("t1")
    assert steps and steps[0].mode == "text" and steps[0].workspace is None
    assert store.draft_version("t1") == 0
    store.migrate()  # 可重复执行
    connection.close()


def test_startup_marks_unfinished_tasks_interrupted(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "t.db")
    store = TaskStore(connection)
    store.migrate()
    running = store.create_task(
        entry="qq", group_id=200, user_id=100, agent="deepseek", prompt="x", status="running"
    )
    waiting = store.create_task(
        entry="qq", group_id=200, user_id=100, agent="deepseek", prompt="y",
        status="awaiting_selection",
    )
    done = store.create_task(
        entry="qq", group_id=200, user_id=100, agent="deepseek", prompt="z", status="succeeded"
    )
    assert store.mark_interrupted_on_startup() == 2
    assert store.get_task(running)["status"] == "interrupted"
    assert store.get_task(waiting)["status"] == "interrupted"
    assert store.get_task(done)["status"] == "succeeded"  # 历史终态不被改写
    connection.close()


def test_late_events_cannot_overwrite_terminal_status(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "t.db")
    store = TaskStore(connection)
    store.migrate()
    task_id = store.create_task(
        entry="team", group_id=200, user_id=100, agent="deepseek", prompt="x", status="running"
    )
    assert store.update_status_if_active(task_id, "cancelled")
    assert not store.update_status_if_active(task_id, "succeeded")
    assert store.get_task(task_id)["status"] == "cancelled"
    connection.close()


def test_file_changes_are_recorded_per_task(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "t.db")
    store = TaskStore(connection)
    store.migrate()
    task_id = store.create_task(
        entry="qq", group_id=200, user_id=100, agent="deepseek", prompt="x", status="running"
    )
    store.record_changes(task_id=task_id, changes=[{
        "path": "/tmp/a.txt", "kind": "modify", "status": "applied",
        "before_hash": "a", "after_hash": "b", "backup": "/tmp/a.txt.bak", "bytes": 3,
    }])
    rows = store.list_changes(task_id=task_id)
    assert rows[0]["backup"].endswith(".bak")
    assert rows[0]["after_hash"] == "b"
    connection.close()


def test_confirmations_do_not_survive_restart() -> None:
    from app.security.confirmations import ConfirmationKind, InMemoryConfirmationStore

    first = InMemoryConfirmationStore(default_ttl_seconds=60)
    record = first.create(
        kind=ConfirmationKind.WORKSPACE_SELECT, user_id=100, group_id=200,
        summary="选择目录", payload={"candidates": []},
    )
    second = InMemoryConfirmationStore(default_ttl_seconds=60)
    outcome, _ = second.consume_record(
        kind=ConfirmationKind.WORKSPACE_SELECT, user_id=100, code=record.code, group_id=200
    )
    assert outcome.value == "not_found"


def test_confirmation_ttl_and_scope_enforced() -> None:
    from app.security.confirmations import ConfirmationKind, InMemoryConfirmationStore

    store = InMemoryConfirmationStore(default_ttl_seconds=0.01)
    record = store.create(
        kind=ConfirmationKind.ACTION, user_id=100, group_id=200, summary="确认",
    )
    outcome, _ = store.consume(kind=ConfirmationKind.ACTION, user_id=101, code=record.code)
    assert outcome.value == "wrong_user"
    time.sleep(0.02)
    outcome, _ = store.consume(kind=ConfirmationKind.ACTION, user_id=100, code=record.code)
    assert outcome.value == "expired"

    other = store.create(
        kind=ConfirmationKind.ACTION, user_id=100, group_id=200, summary="确认2",
    )
    outcome, _ = store.consume(
        kind=ConfirmationKind.ACTION, user_id=100, code=other.code, group_id=999
    )
    assert outcome.value == "wrong_scope"


# ---------- 备份一致性 ----------


def test_wal_consistent_backup_contains_committed_rows(tmp_path) -> None:
    """schema 迁移前的备份必须用 SQLite backup API（含 WAL），不能裸复制 db 文件。"""
    live = tmp_path / "bot.db"
    connection = sqlite3.connect(live)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO t (value) VALUES ('committed')")
    connection.commit()
    # 仍有内容停留在 WAL 中
    assert Path(str(live) + "-wal").exists()

    target = tmp_path / "backup.db"
    source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    with destination:
        source.backup(destination)
    destination.close()
    source.close()
    connection.close()

    restored = sqlite3.connect(target)
    rows = restored.execute("SELECT value FROM t").fetchall()
    restored.close()
    assert rows == [("committed",)]


def test_authorizer_default_deny() -> None:
    authz = Authorizer([], [])
    assert not authz.user_allowed(1, 1)
    assert not authz.admin_allowed(1)


# ---------- 凭据来源（.env 必须仍然生效） ----------


def test_credentials_from_settings_are_used_when_env_missing(tmp_path, monkeypatch) -> None:
    """pydantic-settings 读 .env 不会写回 os.environ：后端工厂必须回退到 Settings。"""
    from app.backends.registry import BackendRegistry

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings(
        tmp_path, deepseek_api_key="sk-placeholder-from-dotenv", deepseek_backend="api"
    )
    registry = BackendRegistry.from_settings(settings)
    descriptor = registry.get("deepseek_api").descriptor
    assert descriptor.configured is True
    assert descriptor.credential_env == "DEEPSEEK_API_KEY"


def test_process_env_still_wins_over_settings(tmp_path, monkeypatch) -> None:
    from app.backends.registry import BackendRegistry

    settings = _settings(tmp_path, deepseek_api_key="", deepseek_backend="api")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-process-env")
    registry = BackendRegistry.from_settings(settings)
    assert registry.get("deepseek_api").descriptor.configured is True


def test_missing_credentials_report_unavailable(tmp_path, monkeypatch) -> None:
    from app.backends.registry import BackendRegistry

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings(tmp_path, deepseek_api_key="", deepseek_backend="api")
    registry = BackendRegistry.from_settings(settings)
    descriptor = registry.get("deepseek_api").descriptor
    assert descriptor.configured is False
    assert descriptor.unavailable_reason
