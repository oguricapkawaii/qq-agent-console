"""A14：team 增强权限——默认 text 禁工具；显式 read/edit/exec 真实执行；越权即拒绝。"""

from __future__ import annotations

import json
from pathlib import Path

from app.workspaces.changes import file_hash
from app.workflows.models import TeamStatus
from helpers import (
    requires_platform_sandbox,
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    return root


async def test_default_step_is_text_without_tools(tmp_path) -> None:
    root = _root(tmp_path)
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="纯文本结论")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("默认文本")
        added = await harness.command(f"/team add {team} s1 deepseek -- 分析一下")
        assert "权限 文本（无工具）" in added
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        options = agent.options_log[-1]
        assert options.mode == "text"
        assert options.directory is None
        assert options.workspace_tools is False
    finally:
        await harness.aclose()


@requires_platform_sandbox()
async def test_read_edit_exec_chain_really_runs(tmp_path) -> None:
    root = _root(tmp_path)
    settings = workspace_settings(tmp_path, root=root)
    inspect = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "app.py"}]}),
        json.dumps({"final": "文件里是 print('v1')"}),
    ])
    fix = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "app.py", "content": "print('v2')\n"}]}]}),
        json.dumps({"final": "已改为 v2"}),
    ])
    verify = ScriptedAgent("codex", [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c", "print('verified')"], "timeout": 10}]}),
        json.dumps({"final": "测试通过"}),
    ])
    harness = ConsoleHarness(
        settings, {"deepseek": inspect, "zcode": fix, "codex": verify}
    )
    try:
        before = file_hash(root / "app.py")
        team = harness.create_team("读改测")
        await harness.command(
            f'/team add {team} inspect deepseek --mode read --dir "{root}" -- 读'
        )
        await harness.command(
            f'/team add {team} fix zcode --after inspect --mode edit --dir "{root}" -- 改'
        )
        await harness.command(
            f'/team add {team} verify codex --after fix --mode exec --dir "{root}" -- 测'
        )
        plan = await harness.command(f"/team plan {team}")
        assert "只读" in plan and "读写" in plan and "读写+受控命令" in plan
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        # 真实文件变更 + 真实命令输出
        assert (root / "app.py").read_text(encoding="utf-8") == "print('v2')\n"
        assert file_hash(root / "app.py") != before
        record = harness.workflow_store.get_step_run(team, 1, "fix")
        changes = harness.task_store.list_changes(task_id=record.task_id)
        backup = Path(changes[0]["backup"])
        assert backup.is_file() and backup.read_text(encoding="utf-8") == "print('v1')\n"
        assert not (root / "app.py.bak").exists()   # F04：备份在受控目录
        result = await harness.command(f"/team result {team}")
        assert "测试通过" in result
        # 权限真的按步生效
        assert inspect.options_log[-1].mode == "read"
        assert fix.options_log[-1].mode == "edit"
        assert verify.options_log[-1].mode == "exec"
    finally:
        await harness.aclose()


async def test_step_without_acl_is_refused_not_downgraded(tmp_path) -> None:
    root = _root(tmp_path)
    # 授权根只给 read，不给 edit
    settings = workspace_settings(
        tmp_path, root=root,
        console_workspaces={"roots": [{
            "path": str(root), "modes": ["read"], "admin_modes": ["read"],
        }]},
    )
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "不应执行"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("越权")
        refused = await harness.command(
            f'/team add {team} s1 deepseek --mode edit --dir "{root}" -- 改文件'
        )
        assert "不可用" in refused or "权限" in refused
        assert harness.workflow_store.count_steps(team) == 0
        assert agent.calls == []
    finally:
        await harness.aclose()


async def test_run_is_blocked_when_acl_revoked_after_add(tmp_path) -> None:
    root = _root(tmp_path)
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "不应执行"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("撤权")
        await harness.command(
            f'/team add {team} s1 deepseek --mode edit --dir "{root}" -- 改'
        )
        harness.acl._grants = ()  # noqa: SLF001 - 模拟管理员移除授权
        refused = await harness.command(f"/team run {team}")
        assert "不在授权根内" in refused
        assert harness.workflow_store.get_workflow(team).status == TeamStatus.DRAFT
        assert agent.calls == []
    finally:
        await harness.aclose()


async def test_text_step_using_tools_is_failed(tmp_path) -> None:
    root = _root(tmp_path)
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="结论", tool_log=["Read a.py"])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("文本却用工具")
        harness.add_step(team, "s1", "deepseek", "分析")
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        result = await harness.command(f"/team result {team}")
        assert "工具" in result
    finally:
        await harness.aclose()


async def test_plan_shows_conflicts_and_requires_after(tmp_path) -> None:
    root = _root(tmp_path)
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        team = harness.create_team("冲突提示")
        await harness.command(
            f'/team add {team} a deepseek --mode read --dir "{root}" -- 读'
        )
        await harness.command(
            f'/team add {team} b deepseek --mode edit --dir "{root}" -- 改'
        )
        plan = await harness.command(f"/team plan {team}")
        assert "同目录步骤互斥" in plan
        assert "--after" in plan
    finally:
        await harness.aclose()
