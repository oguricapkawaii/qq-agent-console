"""WorkflowStore 持久化、幂等、权限字段与状态收敛测试（临时 DB，不触碰生产库）。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from app.storage import ContextStore
from app.workflows.models import (
    FrozenChoice,
    FrozenStep,
    PlanSnapshot,
    StepStatus,
    TeamStatus,
)
from app.workflows.store import WorkflowStore, snapshot_from_json


@pytest.fixture
def store(tmp_path):
    context = ContextStore(str(tmp_path / "team.db"))
    workflow = WorkflowStore(context.connection)
    workflow.migrate()
    yield workflow
    context.close()


def make_snapshot(team_id: str, *steps: tuple[str, int], goal: str = "目标") -> PlanSnapshot:
    return PlanSnapshot(
        team_id=team_id,
        goal=goal,
        steps=tuple(
            FrozenStep(
                step_id=step_id, seq=seq, agent="glm", instruction=f"任务{step_id}",
                depends_on=(), choice=FrozenChoice(model="glm-4"),
            )
            for step_id, seq in steps
        ),
    )


def add_step(store, team_id: str, step_id: str, **kwargs) -> bool:
    values = dict(
        team_id=team_id, step_id=step_id, agent="glm", instruction="X",
        depends_on=(), model=None, effort=None, effort_set=False,
    )
    values.update(kwargs)
    return store.replace_step(**values)


def test_migrate_is_idempotent_and_keeps_existing_tables(store):
    store.migrate()
    store.migrate()
    names = {
        row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"context_messages", "user_whitelist", "agent_preferences"} <= names
    assert {
        "team_workflows", "team_steps", "team_dependencies", "team_runs",
        "team_step_runs", "team_events",
    } <= names


def test_migrate_adds_capability_columns_to_legacy_tables(tmp_path):
    """旧库缺列时只补列，不重建、不清空；旧行按 text 解释。"""
    connection = sqlite3.connect(str(tmp_path / "legacy.db"))
    connection.executescript(
        """
        CREATE TABLE team_workflows (
            team_id TEXT PRIMARY KEY, group_id INTEGER NOT NULL, owner_user_id INTEGER NOT NULL,
            goal TEXT NOT NULL, status TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE team_steps (
            team_id TEXT NOT NULL, step_id TEXT NOT NULL, seq INTEGER NOT NULL,
            agent TEXT NOT NULL, instruction TEXT NOT NULL, model TEXT, effort TEXT,
            effort_set INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (team_id, step_id)
        );
        CREATE TABLE team_step_runs (
            team_id TEXT NOT NULL, round INTEGER NOT NULL, step_id TEXT NOT NULL,
            status TEXT NOT NULL, source_round INTEGER, task_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, answer TEXT NOT NULL DEFAULT '',
            error TEXT, model TEXT, effort TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
            started_at REAL, finished_at REAL, PRIMARY KEY (team_id, round, step_id)
        );
        INSERT INTO team_workflows VALUES ('t7a91c20d', 1, 2, '旧目标', 'draft', 0, 1.0, 1.0);
        INSERT INTO team_steps VALUES ('t7a91c20d', 'a', 0, 'glm', '旧步骤', 'm', NULL, 0);
        INSERT INTO team_step_runs VALUES
            ('t7a91c20d', 1, 'a', 'succeeded', NULL, 'tid', 1, '答案', NULL, 'm', NULL, 3, 1.0, 2.0);
        """
    )
    workflow = WorkflowStore(connection)
    workflow.migrate()

    step_columns = {row[1] for row in connection.execute("PRAGMA table_info(team_steps)")}
    assert {"mode", "backend", "workspace", "find_keyword"} <= step_columns
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(team_step_runs)")}
    assert {"change_fingerprint", "changed_files", "commands"} <= run_columns
    workflow_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(team_workflows)")
    }
    assert "draft_version" in workflow_columns

    step = workflow.list_steps("t7a91c20d")[0]
    assert step.instruction == "旧步骤"  # 旧数据保留
    assert step.mode == MODE_TEXT and step.workspace is None and step.backend is None
    record = workflow.get_step_run("t7a91c20d", 1, "a")
    assert record.status == StepStatus.SUCCEEDED and record.answer == "答案"
    assert record.changed_files == 0 and record.commands == ()
    assert workflow.draft_version("t7a91c20d") == 0
    connection.close()


def test_draft_step_replace_and_dependencies(store):
    record = store.create_draft(group_id=1, owner_user_id=2, goal="目标")
    assert record.team_id.startswith("t") and len(record.team_id) == 9
    assert store.replace_step(
        team_id=record.team_id, step_id="a", agent="glm", instruction="第一版",
        depends_on=(), model=None, effort=None, effort_set=False,
    ) is True
    # 同 ID 完整替换：定义与依赖一起替换
    assert store.replace_step(
        team_id=record.team_id, step_id="a", agent="deepseek", instruction="第二版",
        depends_on=("x",), model="m", effort="high", effort_set=True,
    ) is False
    steps = store.list_steps(record.team_id)
    assert len(steps) == 1
    step = steps[0]
    assert (step.agent, step.instruction, step.model, step.effort, step.effort_set) == (
        "deepseek", "第二版", "m", "high", True,
    )
    assert step.depends_on == ("x",)
    assert store.dependencies(record.team_id) == {"a": {"x"}}
    # 先引用后定义：允许
    store.replace_step(
        team_id=record.team_id, step_id="x", agent="glm", instruction="上游",
        depends_on=(), model=None, effort=None, effort_set=False,
    )
    assert store.list_steps(record.team_id)[0].seq == 0  # 原步骤序号不变


def test_replace_step_keeps_or_replaces_capability_declaration(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(
        store, team_id, "a", mode=MODE_EDIT, backend="b1",
        workspace="/private/tmp/proj", find_keyword="项目",
    )
    step = store.list_steps(team_id)[0]
    assert (step.mode, step.backend, step.workspace, step.find_keyword) == (
        MODE_EDIT, "b1", "/private/tmp/proj", "项目",
    )
    # 同 ID 完整替换必须把能力声明一起替换回去（不能残留旧目录/旧关键词）
    add_step(store, team_id, "a", mode=MODE_TEXT, backend="b2")
    step = store.list_steps(team_id)[0]
    assert (step.mode, step.backend, step.workspace, step.find_keyword) == (
        MODE_TEXT, "b2", None, None,
    )
    # 显式目录替换待选关键词
    add_step(store, team_id, "a", mode=MODE_READ, workspace="/private/tmp/other")
    step = store.list_steps(team_id)[0]
    assert step.workspace == "/private/tmp/other" and step.find_keyword is None


def test_set_step_workspace_clears_keyword_and_keeps_mode(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", mode=MODE_READ, find_keyword="项目")
    add_step(store, team_id, "b", mode=MODE_TEXT)
    assert store.set_step_workspace(
        team_id=team_id, step_id="a", workspace="/private/tmp/proj"
    ) is True
    step = store.list_steps(team_id)[0]
    assert step.workspace == "/private/tmp/proj" and step.find_keyword is None
    assert step.mode == MODE_READ  # 未指定 mode 时保留原声明
    assert store.set_step_workspace(
        team_id=team_id, step_id="a", workspace="/private/tmp/proj2", mode=MODE_EDIT
    ) is True
    assert store.list_steps(team_id)[0].mode == MODE_EDIT
    assert store.set_step_workspace(
        team_id=team_id, step_id="missing", workspace="/private/tmp/x"
    ) is False


def test_draft_version_advances_on_every_draft_change(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    assert store.draft_version(team_id) == 0
    add_step(store, team_id, "a")
    assert store.draft_version(team_id) == 1
    add_step(store, team_id, "a")  # 替换同样算一次草稿变化
    assert store.draft_version(team_id) == 2
    store.set_step_workspace(team_id=team_id, step_id="a", workspace="/private/tmp/x")
    assert store.draft_version(team_id) == 3
    store.remove_step(team_id, "a")
    assert store.draft_version(team_id) == 4
    assert store.draft_version("t00000000") == 0


def test_remove_step_rejects_when_dependents_exist(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="A")
    add_step(store, team_id, "b", instruction="B", depends_on=("a",))
    with pytest.raises(ValueError) as exc:
        store.remove_step(team_id, "a")
    assert "dependents:b" in str(exc.value)
    store.remove_step(team_id, "b")
    store.remove_step(team_id, "a")
    assert store.list_steps(team_id) == []


def test_active_team_cannot_be_edited(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="A")
    assert store.freeze_run(team_id, make_snapshot(team_id, ("a", 0))) == 1
    with pytest.raises(RuntimeError):
        add_step(store, team_id, "b", instruction="B")
    with pytest.raises(RuntimeError):
        store.remove_step(team_id, "a")


def test_freeze_run_is_idempotent(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="A")
    first = store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    second = store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    assert first == 1 and second is None
    assert store.get_workflow(team_id).round == 1
    assert store.get_run(team_id, 1).status == TeamStatus.QUEUED
    # 冻结快照必须带上权限/目录字段，重启后仍按原样解释
    run = store.get_run(team_id, 1)
    payload = json.loads(run.plan_json)
    assert set(payload["steps"][0]) >= {
        "mode", "backend", "workspace", "grant_label", "effort_set",
    }
    snapshot = snapshot_from_json(run.plan_json)
    assert snapshot.steps[0].mode == MODE_TEXT


def test_freeze_run_records_frozen_capability_and_grant_label(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", mode=MODE_EXEC, backend="b1", workspace="/private/tmp/proj")
    snapshot = PlanSnapshot(
        team_id=team_id, goal="目标",
        steps=(FrozenStep(
            step_id="a", seq=0, agent="glm", instruction="任务",
            depends_on=(), choice=FrozenChoice(model="m"),
            mode=MODE_EXEC, backend="b1", workspace="/private/tmp/proj",
            grant_label="test-root",
        ),),
    )
    assert store.freeze_run(team_id, snapshot) == 1
    restored = snapshot_from_json(store.get_run(team_id, 1).plan_json)
    assert restored.steps[0].mode == MODE_EXEC
    assert restored.steps[0].backend == "b1"
    assert restored.steps[0].workspace == "/private/tmp/proj"
    assert restored.steps[0].grant_label == "test-root"


def test_claim_and_finish_are_conditional(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="A")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    assert store.claim_step(team_id, 1, "a") is True
    assert store.claim_step(team_id, 1, "a") is False  # 不会二次执行
    assert store.get_step_run(team_id, 1, "a").attempts == 1
    assert store.finish_step(team_id=team_id, round_no=1, step_id="a",
                             status=StepStatus.SUCCEEDED, answer="答案") is True
    # 晚到的第二次写入不会覆盖
    assert store.finish_step(team_id=team_id, round_no=1, step_id="a",
                             status=StepStatus.FAILED, error="timeout") is False
    record = store.get_step_run(team_id, 1, "a")
    assert record.status == StepStatus.SUCCEEDED and record.answer == "答案"


def test_finish_step_persists_changes_and_commands(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", mode=MODE_EXEC, workspace="/private/tmp/proj")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    store.claim_step(team_id, 1, "a")
    assert store.finish_step(
        team_id=team_id, round_no=1, step_id="a", status=StepStatus.SUCCEEDED,
        answer="完成", task_id="tid1", model="m", effort="high", duration_ms=12,
        change_fingerprint="fp", changed_files=2,
        commands=("broker:run_command ls exit=0",),
    ) is True
    record = store.get_step_run(team_id, 1, "a")
    assert (record.change_fingerprint, record.changed_files) == ("fp", 2)
    assert record.commands == ("broker:run_command ls exit=0",)
    # 未知步骤 / 非 running 状态都不能写入
    assert store.finish_step(
        team_id=team_id, round_no=1, step_id="missing", status=StepStatus.SUCCEEDED
    ) is False
    with pytest.raises(ValueError):
        store.finish_step(
            team_id=team_id, round_no=1, step_id="a", status=StepStatus.RUNNING
        )


def test_cancel_keeps_finished_results_and_rejects_late_writes(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="A")
    add_step(store, team_id, "b", instruction="B", depends_on=("a",))
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0), ("b", 1)))
    assert store.claim_step(team_id, 1, "a")
    assert store.finish_step(team_id=team_id, round_no=1, step_id="a",
                             status=StepStatus.SUCCEEDED, answer="A-答案")
    assert store.claim_step(team_id, 1, "b")
    store.cancel_round(team_id, 1)
    assert store.get_workflow(team_id).status == TeamStatus.CANCELLED
    assert store.get_run(team_id, 1).status == TeamStatus.CANCELLED
    assert store.get_step_run(team_id, 1, "a").status == StepStatus.SUCCEEDED
    assert store.get_step_run(team_id, 1, "b").status == StepStatus.CANCELLED
    # 晚到结果不能把取消翻回成功
    assert store.finish_step(team_id=team_id, round_no=1, step_id="b",
                             status=StepStatus.SUCCEEDED, answer="迟到") is False
    assert store.get_step_run(team_id, 1, "b").status == StepStatus.CANCELLED


def test_complete_round_requires_all_success(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    for step_id, seq in (("a", 0), ("b", 1)):
        add_step(store, team_id, step_id, instruction="X")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0), ("b", 1)))
    store.claim_step(team_id, 1, "a")
    store.finish_step(team_id=team_id, round_no=1, step_id="a", status=StepStatus.SUCCEEDED,
                      answer="ok")
    store.claim_step(team_id, 1, "b")
    store.finish_step(team_id=team_id, round_no=1, step_id="b", status=StepStatus.FAILED,
                      error="timeout")
    assert store.complete_round(team_id, 1) == TeamStatus.FAILED
    assert store.get_workflow(team_id).status == TeamStatus.FAILED


def test_retry_reuses_succeeded_and_only_reruns_rest(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    for step_id, seq in (("a", 0), ("b", 1)):
        add_step(store, team_id, step_id, instruction="X")
    snapshot = make_snapshot(team_id, ("a", 0), ("b", 1))
    store.freeze_run(team_id, snapshot)
    store.claim_step(team_id, 1, "a")
    store.finish_step(team_id=team_id, round_no=1, step_id="a", status=StepStatus.SUCCEEDED,
                      answer="A-答案", model="glm-4")
    store.claim_step(team_id, 1, "b")
    store.finish_step(team_id=team_id, round_no=1, step_id="b", status=StepStatus.FAILED,
                      error="timeout")
    store.complete_round(team_id, 1)
    assert store.start_retry(team_id, snapshot) == 2
    first = store.get_step_run(team_id, 2, "a")
    second = store.get_step_run(team_id, 2, "b")
    assert first.status == StepStatus.SUCCEEDED
    assert first.source_round == 1 and first.answer == "A-答案"
    assert second.status == StepStatus.PENDING and second.source_round is None
    # 原始轮次记录保留
    assert store.get_step_run(team_id, 1, "a").answer == "A-答案"
    # 只有 failed/interrupted 能 retry
    assert store.start_retry(team_id, snapshot) is None


def test_retry_reuse_keeps_change_fingerprint_and_commands(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    for step_id in ("a", "b"):
        add_step(store, team_id, step_id, instruction="X")
    snapshot = make_snapshot(team_id, ("a", 0), ("b", 1))
    store.freeze_run(team_id, snapshot)
    store.claim_step(team_id, 1, "a")
    store.finish_step(
        team_id=team_id, round_no=1, step_id="a", status=StepStatus.SUCCEEDED,
        answer="改完了", change_fingerprint="fp-a", changed_files=1,
        commands=("broker:run_command pytest exit=0",),
    )
    store.claim_step(team_id, 1, "b")
    store.finish_step(team_id=team_id, round_no=1, step_id="b", status=StepStatus.FAILED,
                      error="network")
    store.complete_round(team_id, 1)
    assert store.start_retry(team_id, snapshot) == 2
    reused = store.get_step_run(team_id, 2, "a")
    assert reused.change_fingerprint == "fp-a" and reused.changed_files == 1
    assert reused.commands == ("broker:run_command pytest exit=0",)
    assert reused.source_round == 1


def test_retry_rejected_from_succeeded_and_cancelled(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="X")
    snapshot = make_snapshot(team_id, ("a", 0))
    store.freeze_run(team_id, snapshot)
    store.claim_step(team_id, 1, "a")
    store.finish_step(team_id=team_id, round_no=1, step_id="a", status=StepStatus.SUCCEEDED,
                      answer="ok")
    store.complete_round(team_id, 1)
    assert store.start_retry(team_id, snapshot) is None
    other = store.create_draft(group_id=1, owner_user_id=2, goal="目标2").team_id
    add_step(store, other, "a", instruction="X")
    store.freeze_run(other, make_snapshot(other, ("a", 0)))
    store.cancel_round(other, 1)
    assert store.start_retry(other, make_snapshot(other, ("a", 0))) is None


def test_mark_interrupted_on_startup(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="X")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    store.claim_step(team_id, 1, "a")
    assert store.mark_interrupted_on_startup() == 1
    assert store.get_workflow(team_id).status == TeamStatus.INTERRUPTED
    assert store.get_step_run(team_id, 1, "a").status == StepStatus.INTERRUPTED
    assert store.get_run(team_id, 1).status == TeamStatus.INTERRUPTED
    # 已终态的任务不受影响
    assert store.mark_interrupted_on_startup() == 0


def test_results_in_order_and_recent_teams_isolation(store):
    team_id = store.create_draft(group_id=10, owner_user_id=20, goal="目标").team_id
    for step_id in ("a", "b"):  # seq: a=0, b=1
        add_step(store, team_id, step_id, instruction="X")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0), ("b", 1)))
    store.claim_step(team_id, 1, "b")  # 先完成 b，验证结果仍按计划顺序
    store.finish_step(team_id=team_id, round_no=1, step_id="b", status=StepStatus.SUCCEEDED,
                      answer="B")
    results = store.results_in_order(team_id, 1)
    assert [step.step_id for step, _ in results] == ["a", "b"]  # 按 seq，不按完成顺序
    assert results[0][1].status == StepStatus.PENDING
    assert results[1][1].answer == "B"
    assert store.list_recent_teams(group_id=10, owner_user_id=20)
    assert store.list_recent_teams(group_id=10, owner_user_id=21) == []
    assert store.list_recent_teams(group_id=11, owner_user_id=20) == []


def test_results_in_order_reports_mode_and_directory(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", mode=MODE_READ, workspace="/private/tmp/proj")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    results = store.results_in_order(team_id, 1)
    assert results[0][0].mode == MODE_READ
    assert results[0][0].workspace == "/private/tmp/proj"


def test_interrupt_round_is_retryable_and_keeps_success(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    for step_id in ("a", "b"):
        add_step(store, team_id, step_id, instruction="X")
    snapshot = make_snapshot(team_id, ("a", 0), ("b", 1))
    store.freeze_run(team_id, snapshot)
    store.claim_step(team_id, 1, "a")
    store.finish_step(team_id=team_id, round_no=1, step_id="a", status=StepStatus.SUCCEEDED,
                      answer="已完成")
    store.claim_step(team_id, 1, "b")
    assert store.interrupt_round(team_id, 1) == 1
    assert store.get_workflow(team_id).status == TeamStatus.INTERRUPTED
    assert store.get_step_run(team_id, 1, "a").status == StepStatus.SUCCEEDED
    assert store.get_step_run(team_id, 1, "b").status == StepStatus.INTERRUPTED
    # interrupted 任务可显式 retry，并复用成功结果
    assert store.start_retry(team_id, snapshot) == 2
    assert store.get_step_run(team_id, 2, "a").source_round == 1
    assert store.get_step_run(team_id, 2, "b").status == StepStatus.PENDING


def test_list_active_teams_filters_group_and_owner(store):
    first = store.create_draft(group_id=10, owner_user_id=20, goal="A").team_id
    second = store.create_draft(group_id=10, owner_user_id=21, goal="B").team_id
    for team_id in (first, second):
        add_step(store, team_id, "a", instruction="X")
        store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    assert [item.team_id for item in store.list_active_teams(group_id=10, owner_user_id=20)] == [first]
    assert store.list_active_teams(group_id=11, owner_user_id=20) == []
    store.cancel_round(first, 1)
    assert store.list_active_teams(group_id=10, owner_user_id=20) == []


def test_active_counts_and_events(store):
    first = store.create_draft(group_id=1, owner_user_id=2, goal="A").team_id
    second = store.create_draft(group_id=1, owner_user_id=3, goal="B").team_id
    for team_id in (first, second):
        add_step(store, team_id, "a", instruction="X")
        store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    assert store.count_active_global() == 2
    assert store.count_active_user(2) == 1
    assert store.count_active_user(3) == 1
    events = store._conn.execute(
        "SELECT event FROM team_events WHERE team_id=? ORDER BY id", (first,)
    ).fetchall()
    assert [row[0] for row in events] == ["create", "step_create", "run"]
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT * FROM team_workflows WHERE team_id=?", ("x", "y"))


def test_step_replace_is_recorded_as_audit_event(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="A").team_id
    add_step(store, team_id, "a", instruction="第一版")
    add_step(store, team_id, "a", instruction="第二版", mode=MODE_EDIT)
    events = store._conn.execute(
        "SELECT event, detail FROM team_events WHERE team_id=? AND step_id='a' ORDER BY id",
        (team_id,),
    ).fetchall()
    assert [row[0] for row in events] == ["step_create", "step_replace"]
    assert all("agent=glm" in row[1] for row in events)


def test_cancelled_round_not_overwritten_by_late_completion(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="X")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0)))
    store.cancel_round(team_id, 1)
    # 晚到的完成/失败信号不能翻转已取消的轮次
    assert store.complete_round(team_id, 1) == TeamStatus.CANCELLED
    assert store.fail_round(team_id, 1, "internal") is False
    assert store.get_workflow(team_id).status == TeamStatus.CANCELLED
    assert store.get_run(team_id, 1).status == TeamStatus.CANCELLED


def test_fail_round_marks_unscheduled_steps_failed(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    for step_id in ("a", "b"):
        add_step(store, team_id, step_id, instruction="X")
    store.freeze_run(team_id, make_snapshot(team_id, ("a", 0), ("b", 1)))
    assert store.fail_round(team_id, 1, "workflow_timeout") is True
    assert store.get_workflow(team_id).status == TeamStatus.FAILED
    assert store.get_step_run(team_id, 1, "a").status == StepStatus.FAILED
    assert store.get_step_run(team_id, 1, "b").error == "workflow_timeout"
    assert store.fail_round(team_id, 1, "internal") is False


def test_cancel_draft_only_affects_draft(store):
    team_id = store.create_draft(group_id=1, owner_user_id=2, goal="目标").team_id
    add_step(store, team_id, "a", instruction="X")
    assert store.cancel_draft(team_id) is True
    assert store.get_workflow(team_id).status == TeamStatus.CANCELLED
    assert store.cancel_draft(team_id) is False
    # 已取消的草稿不能启动
    assert store.freeze_run(team_id, make_snapshot(team_id, ("a", 0))) is None
    assert store.get_workflow(team_id).round == 0
