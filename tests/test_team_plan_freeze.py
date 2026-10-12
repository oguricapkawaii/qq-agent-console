"""A11/A15：团队计划的目录与能力冻结、find 未确认不启动、草稿变更使旧确认失效。"""

from __future__ import annotations

import json
from pathlib import Path

from app.workflows.models import TeamStatus
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)


def _workspace(tmp_path: Path, name: str = "工作区") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "a.txt").write_text("v1", encoding="utf-8")
    return root


# ---------- 冻结 ----------


async def test_run_freezes_backend_model_mode_and_path(tmp_path) -> None:
    root = _workspace(tmp_path)
    settings = workspace_settings(tmp_path, root=root)
    # V01：read 步骤必须有真实动作回执，"只有 final"会被记为 no_operation 失败
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "完成"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("冻结")
        harness.add_step(team, "s1", "deepseek", "读", mode="read",
                         workspace=str(root))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        run = harness.workflow_store.get_run(team, 1)
        from app.workflows.store import snapshot_from_json

        step = snapshot_from_json(run.plan_json).steps[0]
        assert step.mode == "read"
        assert step.backend == "deepseek"
        assert step.workspace == str(root.resolve())
        assert step.grant_label  # 冻结时记录了授权引用
        # 冻结后修改用户默认，不影响已冻结轮次
        await harness.command("/agent set deepseek --model fake-deepseek")
        assert snapshot_from_json(
            harness.workflow_store.get_run(team, 1).plan_json
        ).steps[0].choice.model == step.choice.model
    finally:
        await harness.aclose()


async def test_unresolved_find_keeps_plan_draft_and_blocks_run(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "目标项目").mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "完成"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("待选目录")
        added = await harness.command(f'/team add {team} s1 deepseek --find "目标" --mode read -- 读')
        assert "/workspace select" in added
        plan = await harness.command(f"/team plan {team}")
        assert "目录待选择" in plan
        refused = await harness.command(f"/team run {team}")
        assert "尚未选择" in refused
        assert "未开始任何一步" in refused
        assert harness.workflow_store.get_workflow(team).status == TeamStatus.DRAFT
        assert agent.calls == []
    finally:
        await harness.aclose()


async def test_selection_then_run_uses_confirmed_path(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    target = root / "目标项目"
    target.mkdir()
    (target / "a.txt").write_text("x", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "读完"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("选择后运行")
        await harness.command(f'/team add {team} s1 deepseek --find "目标" --mode read -- 读')
        code = _code(harness)
        selected = await harness.command(f"/workspace select {code} 1")
        assert "已冻结目录" in selected
        plan = await harness.command(f"/team plan {team}")
        assert str(target.resolve()) in plan
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
    finally:
        await harness.aclose()


async def test_draft_change_invalidates_old_selection(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "目标项目").mkdir()
    (root / "另一项目").mkdir()
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        team = harness.create_team("草稿变化")
        await harness.command(f'/team add {team} s1 deepseek --find "目标" --mode read -- 读')
        code = _code(harness)
        # 修改草稿（替换同一步骤）→ 草稿版本变化，旧确认失效
        await harness.command(f'/team add {team} s1 deepseek --find "另一" --mode read -- 重来')
        result = await harness.command(f"/workspace select {code} 1")
        assert "失效" in result or "无效" in result
    finally:
        await harness.aclose()


async def test_rebinding_does_not_change_frozen_path(tmp_path) -> None:
    first = tmp_path / "第一目录"
    second = tmp_path / "第二目录"
    first.mkdir()
    second.mkdir()
    settings = workspace_settings(tmp_path, root=tmp_path)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "ok"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("冻结路径")
        harness.add_step(team, "s1", "deepseek", "读", mode="read",
                         workspace=str(first.resolve()))
        await harness.command(f"/team run {team}")
        await harness.wait_team(team)
        await harness.command(f'/workspace use "{second}"')
        from app.workflows.store import snapshot_from_json

        run = harness.workflow_store.get_run(team, 1)
        assert snapshot_from_json(run.plan_json).steps[0].workspace == str(first.resolve())
    finally:
        await harness.aclose()


def _code(harness: ConsoleHarness) -> str:
    for _group, text in reversed(harness.transport.sends):
        if "/workspace select" in text:
            return text.split("/workspace select")[1].split()[0]
    raise AssertionError("回执中没有确认码")
