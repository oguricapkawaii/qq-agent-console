"""统一能力契约：取代 V1「团队步骤永远只能是文本」的静态断言。

三条不可回退的行为（对应用户可观察的权限边界）：
(a) 未声明能力的步骤没有工具、没有目录，拿到的是 text-only 规格；
(b) 显式 --mode read/edit/exec 的步骤真实授权并按声明执行（broker 落盘/受控命令）；
(c) 声明了但缺少目录 ACL 的步骤被明确拒绝，绝不静默降级成 text/read；

另外保持两条隔离约束：team 步骤不读写普通聊天历史（系统提示词与历史都为空）、
上游文本是数据而不是权限。
"""

from __future__ import annotations

import json

import pytest

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from app.security.paths import canonical
from app.workflows.executor import STEP_SYSTEM_PROMPT
from app.workflows.models import StepStatus, TeamStatus
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    FakeAgent,
    ScriptedAgent,
    TeamHarness,
    workspace_settings,
)

MEMBER = 700


def _workspaces(root, modes=("read", "edit", "exec"), admin_modes=None, label="test-root"):
    """本文件局部助手：构造授权根配置。"""
    return {
        "roots": [{
            "path": str(root),
            "modes": list(modes),
            "admin_modes": list(modes if admin_modes is None else admin_modes),
            "searchable": True,
            "label": label,
        }],
        "find": {"max_depth": 3, "max_candidates": 5, "max_seconds": 2.0},
    }


def _edit_actions(path: str, content: str) -> str:
    return json.dumps({"actions": [{"action": "propose_changes", "changes": [
        {"path": path, "content": content}
    ]}]}, ensure_ascii=False)


@pytest.fixture
async def env(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    agents = {
        "plain": FakeAgent("plain", reply="纯文本答复"),
        "reader": ScriptedAgent("reader", [
            json.dumps({"actions": [{"action": "list", "path": "."}]}),
            json.dumps({"final": "只读完成"}),
        ]),
        "writer": ScriptedAgent("writer", [
            _edit_actions("a.txt", "v2"),
            json.dumps({"final": "写入完成"}),
        ]),
        "runner": ScriptedAgent("runner", [
            json.dumps({"actions": [{"action": "run_command", "argv": ["ls"], "cwd": "."}]}),
            json.dumps({"final": "命令完成"}),
        ]),
    }
    harness = TeamHarness(workspace_settings(tmp_path, root=root), agents)
    harness.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
    harness.root = root
    yield harness
    await harness.aclose()


def spy_on_gateway(monkeypatch, harness) -> dict:
    """捕获每次执行的 (spec, history, broker)，用于验证能力规格。"""
    captured: dict = {}
    original = harness.gateway.execute

    async def spy(*, spec, prompt, history=(), broker=None):
        captured[spec.step_id or spec.agent] = {
            "spec": spec, "history": tuple(history), "broker": broker,
        }
        return await original(spec=spec, prompt=prompt, history=history, broker=broker)

    monkeypatch.setattr(harness.gateway, "execute", spy)
    return captured


# ---------- (a) 默认步骤 = text-only 规格 ----------


async def test_default_step_gets_text_only_spec(env, monkeypatch):
    captured = spy_on_gateway(monkeypatch, env)
    # 即使存在聊天历史，team 步骤也不读它
    env.context_store.append(
        group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="plain",
        role="user", content="聊天历史：机密",
    )
    team_id = env.create_team()
    env.add_step(team_id, "t", "plain")
    step = env.workflow_store.list_steps(team_id)[0]
    assert (step.mode, step.workspace, step.find_keyword) == (MODE_TEXT, None, None)
    await env.command(f"/team run {team_id}")
    assert await env.wait_team(team_id) == TeamStatus.SUCCEEDED

    entry = captured["t"]
    spec = entry["spec"]
    assert spec.mode == MODE_TEXT
    assert spec.workspace is None
    assert spec.use_broker is False and spec.use_native is False
    assert entry["broker"] is None
    assert entry["history"] == ()  # 不读聊天历史
    assert spec.system_prompt == STEP_SYSTEM_PROMPT  # 使用隔离的步骤提示词
    assert spec.entry == "team" and spec.team_id == team_id and spec.step_id == "t"

    options = env.agents["plain"].options_log[-1]
    assert options.mode == MODE_TEXT and options.directory is None
    assert options.workspace_tools is False
    assert "聊天历史：机密" not in env.agents["plain"].last_prompt()
    record = env.workflow_store.get_step_run(team_id, 1, "t")
    assert record.status == StepStatus.SUCCEEDED
    assert record.changed_files == 0 and record.commands == ()


async def test_text_step_using_tools_fails_instead_of_upgrading(tmp_path):
    """文本步骤出现工具调用：如实失败，不静默当作成功，也不升级权限。"""
    root = tmp_path / "proj"
    root.mkdir()
    harness = TeamHarness(workspace_settings(tmp_path, root=root), {
        "tooled": FakeAgent("tooled", reply="我读了文件", tool_log=["Read /etc/passwd"]),
    })
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "t", "tooled")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id) == TeamStatus.FAILED
        record = harness.workflow_store.get_step_run(team_id, 1, "t")
        assert record.status == StepStatus.FAILED and record.error == "tool_used"
        assert record.changed_files == 0
    finally:
        await harness.aclose()


# ---------- (b) 显式 read/edit/exec 真实执行 ----------


async def test_explicit_modes_are_authorized_and_executed(env, monkeypatch):
    captured = spy_on_gateway(monkeypatch, env)
    root = env.root
    expected_dir = str(canonical(str(root)))
    team_id = env.create_team()
    receipts = {
        "r": await env.command(
            f'/team add {team_id} r reader --mode read --dir "{root}" -- 只读检查'
        ),
        "e": await env.command(
            f'/team add {team_id} e writer --after r --mode edit --dir "{root}" -- 写入'
        ),
        "x": await env.command(
            f'/team add {team_id} x runner --after e --mode exec --dir "{root}" -- 运行命令'
        ),
    }
    assert "已添加步骤 r" in receipts["r"] and "权限 只读" in receipts["r"]
    assert "权限 读写" in receipts["e"]
    assert "权限 读写+受控命令" in receipts["x"]
    for step in env.workflow_store.list_steps(team_id):
        assert step.workspace == expected_dir  # add 时已规范化

    assert "已排入后台执行" in await env.command(f"/team run {team_id}")
    assert await env.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED

    for step_id, mode in (("r", MODE_READ), ("e", MODE_EDIT), ("x", MODE_EXEC)):
        spec = captured[step_id]["spec"]
        assert spec.mode == mode
        assert str(spec.workspace) == expected_dir
        assert spec.use_broker is True and captured[step_id]["broker"] is not None
        assert captured[step_id]["history"] == ()
        options = env.agents[{"r": "reader", "e": "writer", "x": "runner"}[step_id]].options_log[0]
        assert options.mode == mode and options.directory == expected_dir

    runs = env.workflow_store.list_step_runs(team_id, 1)
    assert runs["r"].status == StepStatus.SUCCEEDED and runs["r"].changed_files == 0
    assert runs["e"].status == StepStatus.SUCCEEDED and runs["e"].changed_files == 1
    assert runs["e"].change_fingerprint
    assert runs["x"].status == StepStatus.SUCCEEDED
    assert runs["x"].commands == ("broker:run_command ls exit=0",)
    assert (root / "a.txt").read_text(encoding="utf-8") == "v2"


async def test_run_receipt_and_status_show_declared_mode(env):
    root = env.root
    team_id = env.create_team()
    await env.command(f'/team add {team_id} e writer --mode edit --dir "{root}" -- 写入')
    run_text = await env.command(f"/team run {team_id}")
    assert "权限 读写" in run_text and str(canonical(str(root))) in run_text
    assert await env.wait_team(team_id) == TeamStatus.SUCCEEDED
    status = await env.command(f"/team status {team_id}")
    assert "· 读写 ·" in status and "变更 1 个文件" in status


# ---------- (c) 缺少 ACL：拒绝而不是降级 ----------


async def test_step_without_directory_acl_is_refused(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    harness = TeamHarness(
        workspace_settings(
            tmp_path, root=root,
            console_workspaces=_workspaces(root, modes=("read",)),
        ),
        {"writer": ScriptedAgent("writer", [_edit_actions("a.txt", "v2")])},
    )
    try:
        harness.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
        team_id = harness.create_team(group_id=ALLOWED_GROUP, user_id=MEMBER)
        refused = await harness.command(
            f'/team add {team_id} e writer --mode edit --dir "{root}" -- 写入', user_id=MEMBER
        )
        assert "目录不可用" in refused and "只有 read" in refused
        assert harness.workflow_store.count_steps(team_id) == 0
        # 只有 read 权限时 read 步骤仍然可用（拒绝的是越权，不是目录能力本身）
        ok = await harness.command(
            f'/team add {team_id} r writer --mode read --dir "{root}" -- 读取', user_id=MEMBER
        )
        assert "已添加步骤 r" in ok and "权限 只读" in ok
        assert harness.workflow_store.list_steps(team_id)[0].mode == MODE_READ
        assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    finally:
        await harness.aclose()


async def test_acl_narrowed_after_add_refuses_run(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = TeamHarness(workspace_settings(tmp_path, root=root), {
        "writer": ScriptedAgent("writer", [_edit_actions("a.txt", "v2")]),
    })
    team_id = first.create_team()
    await first.command(f'/team add {team_id} e writer --mode edit --dir "{root}" -- 写入')
    first.context_store.close()

    second = TeamHarness(
        workspace_settings(
            tmp_path, root=root,
            console_workspaces=_workspaces(root, modes=("read",)),
        ),
        {"writer": ScriptedAgent("writer", [_edit_actions("a.txt", "v2")])},
    )
    try:
        text = await second.command(f"/team run {team_id}")
        assert "只有 read" in text and "edit" in text
        assert second.workflow_store.get_workflow(team_id).status == TeamStatus.DRAFT
        assert second.workflow_store.get_workflow(team_id).round == 0
        assert second.agents["writer"].calls == []
        assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    finally:
        await second.aclose()
