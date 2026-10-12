"""协作模型、拓扑校验与计划快照契约测试。

重点：
- DAG 分层/未定义依赖/下游闭包等纯函数保持稳定顺序；
- StepSpec/FrozenStep 新增 mode/backend/workspace/find_keyword/grant_label 字段的默认值；
- 计划快照 JSON 往返保留冻结的权限/目录信息；
- 旧（V1）冻结快照缺字段时按 text 解释，绝不因此获得文件权限。
"""

from __future__ import annotations

import json

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from app.workflows.models import (
    FrozenChoice,
    FrozenStep,
    PlanSnapshot,
    StepSpec,
    StepStatus,
    TeamStatus,
    dependents_map,
    downstream_closure,
    find_undefined_dependencies,
    topological_layers,
    validate_agent_name,
    validate_step_id,
    validate_team_id,
)
from app.workflows.store import snapshot_from_json, snapshot_to_json


def test_step_id_and_team_id_validation():
    assert validate_step_id("research")
    assert validate_step_id("a")
    assert validate_step_id("a_b-c9")
    assert not validate_step_id("Research")
    assert not validate_step_id("1abc")
    assert not validate_step_id("a" * 33)
    assert not validate_step_id("")
    assert validate_team_id("t7a91c20d")
    assert not validate_team_id("t7A91C20D")
    assert not validate_team_id("7a91c20d")
    assert validate_agent_name("deepseek")
    assert not validate_agent_name("DeepSeek")


def test_topological_layers_stable_and_parallel():
    steps = ["a", "b", "c", "d"]
    deps = {"a": set(), "b": set(), "c": {"a"}, "d": {"a", "b"}}
    layers = topological_layers(steps, deps)
    assert layers == [["a", "b"], ["c", "d"]]
    # 输入顺序变化只影响层内顺序，不改变分层
    assert topological_layers(["b", "a", "d", "c"], deps) == [["b", "a"], ["d", "c"]]


def test_topological_layers_detects_unknown_cycle_and_self_loop():
    assert topological_layers(["a", "b"], {"a": {"missing"}, "b": set()}) is None
    assert topological_layers(["a", "b"], {"a": {"b"}, "b": {"a"}}) is None
    assert topological_layers(["a"], {"a": {"a"}}) is None
    assert topological_layers(["a", "b"], {"a": set(), "b": {"a"}}) == [["a"], ["b"]]


def test_find_undefined_dependencies_is_stable():
    steps = ["a", "b", "c"]
    deps = {"a": set(), "b": {"x", "a"}, "c": set()}
    assert find_undefined_dependencies(steps, deps) == [("b", "x")]


def test_dependents_and_downstream_closure():
    steps = ["a", "b", "c", "d", "e"]
    deps = {"a": set(), "b": {"a"}, "c": {"a"}, "d": {"b"}, "e": {"c"}}
    children = dependents_map(steps, deps)
    assert children["a"] == ["b", "c"]
    assert downstream_closure(steps, deps, "a") == ["b", "c", "d", "e"]
    assert downstream_closure(steps, deps, "b") == ["d"]
    assert downstream_closure(steps, deps, "d") == []


def test_step_spec_defaults_are_text_without_workspace():
    spec = StepSpec(step_id="a", seq=0, agent="glm", instruction="任务")
    assert spec.depends_on == ()
    assert spec.model is None and spec.effort is None and spec.effort_set is False
    assert spec.mode == MODE_TEXT
    assert spec.backend is None and spec.workspace is None and spec.find_keyword is None


def test_step_spec_carries_declared_capability():
    spec = StepSpec(
        step_id="a", seq=1, agent="glm", instruction="改文件", depends_on=("up",),
        model="m", effort="high", effort_set=True, mode=MODE_EDIT,
        backend="b1", workspace="/tmp/项目", find_keyword=None,
    )
    assert (spec.mode, spec.backend, spec.workspace) == (MODE_EDIT, "b1", "/tmp/项目")


def test_frozen_step_needs_workspace_follows_mode():
    for mode, expected in (
        (MODE_TEXT, False), (MODE_READ, True), (MODE_EDIT, True), (MODE_EXEC, True),
    ):
        step = FrozenStep(
            step_id="a", seq=0, agent="glm", instruction="任务", depends_on=(),
            choice=FrozenChoice(model="m"), mode=mode,
        )
        assert step.needs_workspace is expected, mode
    assert FrozenStep(
        step_id="a", seq=0, agent="glm", instruction="任务", depends_on=(),
        choice=FrozenChoice(model="m"),
    ).workspace_path() is None
    assert FrozenStep(
        step_id="a", seq=0, agent="glm", instruction="任务", depends_on=(),
        choice=FrozenChoice(model="m"), mode=MODE_READ, workspace="/tmp/x",
    ).workspace_path() is not None


def test_plan_snapshot_step_ids_preserve_plan_order():
    snapshot = PlanSnapshot(
        team_id="t7a91c20d", goal="目标",
        steps=(
            FrozenStep(step_id="b", seq=1, agent="glm", instruction="B", depends_on=(),
                       choice=FrozenChoice(model="m")),
            FrozenStep(step_id="a", seq=0, agent="glm", instruction="A", depends_on=(),
                       choice=FrozenChoice(model="m")),
        ),
    )
    # step_ids 只按 tuple 顺序输出；调用方（store）负责按 seq 排序
    assert snapshot.step_ids() == ("b", "a")


def test_plan_snapshot_json_roundtrip_keeps_capability():
    snapshot = PlanSnapshot(
        team_id="t7a91c20d",
        goal="目标",
        steps=(
            FrozenStep(
                step_id="a", seq=0, agent="deepseek", instruction="第一行\n第二行",
                depends_on=(), choice=FrozenChoice(model="m", effort=None, effort_set=True),
                mode=MODE_READ, backend="b1", workspace="/private/tmp/x",
                grant_label="test-root",
            ),
            FrozenStep(
                step_id="b", seq=1, agent="glm", instruction="汇总",
                depends_on=("a",), choice=FrozenChoice(model="g", effort="high"),
                mode=MODE_EXEC, backend="b2", workspace="/private/tmp/x",
                grant_label="test-root",
            ),
        ),
    )
    restored = snapshot_from_json(snapshot_to_json(snapshot))
    assert restored == snapshot
    assert [step.mode for step in restored.steps] == [MODE_READ, MODE_EXEC]
    assert restored.steps[0].grant_label == "test-root"


def test_legacy_snapshot_without_capability_fields_is_text_only():
    """V1 冻结快照没有 mode/backend/workspace/effort_set：按 text 解释。"""
    legacy = json.dumps({
        "team_id": "t7a91c20d",
        "goal": "旧目标",
        "steps": [
            {
                "step_id": "a", "seq": 0, "agent": "glm", "instruction": "旧步骤",
                "depends_on": [], "model": "m", "effort": "high",
            },
            {
                "step_id": "b", "seq": 1, "agent": "glm", "instruction": "旧下游",
                "depends_on": ["a"], "model": "m", "effort": None, "mode": "", "workspace": None,
            },
        ],
    }, ensure_ascii=False)
    snapshot = snapshot_from_json(legacy)
    assert [step.step_id for step in snapshot.steps] == ["a", "b"]
    for step in snapshot.steps:
        assert step.mode == MODE_TEXT
        assert step.backend is None and step.workspace is None
        assert step.grant_label == ""
        assert step.needs_workspace is False
    assert snapshot.steps[1].depends_on == ("a",)
    # effort_set 缺失按 False 解释（沿用偏好语义），但不影响权限
    assert snapshot.steps[0].choice.effort == "high"
    assert snapshot.steps[0].choice.effort_set is False


def test_status_constants_cover_plan_and_step_lifecycle():
    assert set(TeamStatus.ALL) == {
        "draft", "queued", "running", "succeeded", "failed", "cancelled", "interrupted",
    }
    assert set(TeamStatus.ACTIVE) == {"queued", "running"}
    assert set(StepStatus.TERMINAL) == {
        "succeeded", "failed", "cancelled", "interrupted", "blocked",
    }
    assert set(StepStatus.UNSCHEDULED) == {"pending", "ready"}
