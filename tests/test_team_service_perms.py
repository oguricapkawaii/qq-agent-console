"""权限、配额、目录冻结与命令语义测试（跨群/跨用户/管理员边界）。

覆盖两类契约：
- 可见性与生命周期：按「群 × 发起人」隔离、run 幂等、cancel/retry 语义、配额与限流；
- 能力声明：--dir 在 add 时规范化+授权（用户 ACL ∩ 步骤声明 ∩ 后端能力 ∩ 全局策略），
  --find 保持未选定（计划保持 draft、run 整体拒绝），缺少 ACL 的步骤被明确拒绝
  （绝不静默降级为 text/read）。
"""

from __future__ import annotations

import json

import pytest

from app.commands.options import MODE_EDIT, MODE_READ, MODE_TEXT
from app.security.paths import canonical
from app.workflows.models import StepStatus, TeamStatus
from app.workflows.service import NOT_FOUND_TEXT
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    FakeAgent,
    ScriptedAgent,
    TeamHarness,
    make_team_settings,
    workspace_settings,
)

MEMBER = 700
OTHER_GROUP = 201


def _workspaces(root, modes=("read", "edit", "exec"), admin_modes=None, label="test-root"):
    """本文件局部助手：构造一个授权根配置（默认成员/管理员同权）。"""
    return {
        "roots": [
            {
                "path": str(root),
                "modes": list(modes),
                "admin_modes": list(modes if admin_modes is None else admin_modes),
                "searchable": True,
                "label": label,
            }
        ],
        "find": {"max_depth": 3, "max_candidates": 5, "max_seconds": 2.0},
    }


@pytest.fixture
async def harness(tmp_path):
    settings = make_team_settings(
        tmp_path, team_max_active_per_user=1, team_max_active_global=4
    )
    agents = {
        "glm": FakeAgent("glm", reply="A"),
        "deepseek": FakeAgent(
            "deepseek", reply="B", model_options={"fake-deepseek": ("low", "high")}
        ),
        "zcode": FakeAgent("zcode", reply="Z"),
        "codex": FakeAgent("codex", configured=False),
        "off": FakeAgent("off", enabled=False),
    }
    env = TeamHarness(settings, agents)
    env.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
    yield env
    await env.aclose()


@pytest.fixture
async def file_harness(tmp_path):
    """带一个可读写授权根的完整装配（成员与管理员 read/edit/exec）。"""
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agents = {
        "glm": FakeAgent("glm", reply="A"),
        "reader": ScriptedAgent("reader", [
            json.dumps({"actions": [{"action": "list", "path": "."}]}),
            json.dumps({"final": "读完了"}),
        ]),
    }
    env = TeamHarness(settings, agents)
    env.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
    env.root = root
    yield env
    await env.aclose()


async def create_team(harness, user_id=ALLOWED_USER, group_id=ALLOWED_GROUP) -> str:
    text = await harness.command("/team create 目标", user_id=user_id, group_id=group_id)
    return text.split()[2]


# ---------- 可见性与写保护 ----------


async def test_cross_user_and_cross_group_are_invisible(harness):
    team_id = await create_team(harness)
    await harness.command(f"/team add {team_id} a glm -- 步骤")
    for command in (
        f"/team plan {team_id}",
        f"/team run {team_id}",
        f"/team status {team_id}",
        f"/team result {team_id}",
        f"/team cancel {team_id}",
        f"/team retry {team_id}",
        f"/team add {team_id} b glm -- 注入",
        f"/team remove {team_id} a",
    ):
        text = await harness.command(command, user_id=MEMBER)
        assert text == NOT_FOUND_TEXT, command
    # 其他群同样不可见
    harness.authz._groups = frozenset({ALLOWED_GROUP, OTHER_GROUP})
    text = await harness.command(f"/team status {team_id}", group_id=OTHER_GROUP)
    # 其他群未配置白名单用户：命令被授权前置拦截或返回统一文案
    assert team_id not in text or text == NOT_FOUND_TEXT
    # 管理员也不能控制他人任务
    other = await create_team(harness, user_id=MEMBER)
    assert await harness.command(f"/team status {other}") == NOT_FOUND_TEXT
    assert await harness.command(f"/team cancel {other}") == NOT_FOUND_TEXT
    assert await harness.command(f"/team add {other} x glm -- 越权") == NOT_FOUND_TEXT
    assert harness.agents["glm"].calls == []


async def test_unsupported_edits_after_run(harness):
    team_id = await create_team(harness)
    await harness.command(f"/team add {team_id} a glm -- 步骤")
    await harness.command(f"/team run {team_id}")
    assert await harness.wait_team(team_id) == TeamStatus.SUCCEEDED
    assert "不能再修改步骤" in await harness.command(f"/team add {team_id} b glm -- 新步骤")
    assert "不能再删除步骤" in await harness.command(f"/team remove {team_id} a")
    # 重复 run 不新建轮次
    before = harness.workflow_store.get_workflow(team_id).round
    text = await harness.command(f"/team run {team_id}")
    assert "无需重复启动" in text
    assert harness.workflow_store.get_workflow(team_id).round == before


# ---------- 步骤声明与授权 ----------


async def test_unknown_disabled_unconfigured_and_invalid_options_rejected(harness):
    team_id = await create_team(harness)
    assert "未知 Agent" in await harness.command(f"/team add {team_id} a nope -- 步骤")
    assert "已禁用" in await harness.command(f"/team add {team_id} a off -- 步骤")
    text = await harness.command(f"/team add {team_id} a codex -- 步骤")
    assert "未配置" in text
    assert "不在" in await harness.command(
        f"/team add {team_id} a deepseek --model nope -- 步骤"
    )
    assert "不受模型" in await harness.command(
        f"/team add {team_id} a deepseek --model fake-deepseek --effort ultra -- 步骤"
    )
    # 目录不在授权根内：add 直接拒绝，不落任何降级步骤
    assert "目录不可用" in await harness.command(
        f"/team add {team_id} a glm --dir /tmp --mode read -- 步骤"
    )
    assert harness.workflow_store.count_steps(team_id) == 1  # 只剩 codex 那一步
    # 未配置的 Agent 可以记录为草稿（带警告），但 run 前必须拒绝
    assert [step.agent for step in harness.workflow_store.list_steps(team_id)] == ["codex"]
    assert "尚未配置" in await harness.command(f"/team run {team_id}")
    assert harness.agents["glm"].calls == []


async def test_add_canonicalizes_directory_and_freezes_declared_mode(file_harness):
    root = file_harness.root
    team_id = await create_team(file_harness)
    # 用带冗余分量的写法，验证规范化（真实路径 + 父链）
    receipt = await file_harness.command(
        f'/team add {team_id} r reader --mode read --dir "{root}/./" -- 读'
    )
    assert "已添加步骤 r" in receipt
    assert "权限 只读" in receipt
    step = file_harness.workflow_store.list_steps(team_id)[0]
    assert step.mode == MODE_READ
    assert step.workspace == str(canonical(str(root)))
    assert step.find_keyword is None
    # run 回执与冻结快照都带上声明后的权限与规范目录
    run_text = await file_harness.command(f"/team run {team_id}")
    assert "权限 只读" in run_text and str(canonical(str(root))) in run_text
    assert await file_harness.wait_team(team_id) == TeamStatus.SUCCEEDED
    frozen = json.loads(file_harness.workflow_store.get_run(team_id, 1).plan_json)["steps"][0]
    assert frozen["mode"] == MODE_READ
    assert frozen["workspace"] == str(canonical(str(root)))
    assert frozen["grant_label"] == "test-root"
    assert frozen["backend"] == "reader"


async def test_text_step_has_no_directory_and_gets_text_spec(file_harness):
    team_id = await create_team(file_harness)
    await file_harness.command(f"/team add {team_id} a glm -- 纯文本")
    step = file_harness.workflow_store.list_steps(team_id)[0]
    assert (step.mode, step.workspace, step.find_keyword) == (MODE_TEXT, None, None)
    assert await file_harness.wait_team(team_id) == "draft"
    await file_harness.command(f"/team run {team_id}")
    assert await file_harness.wait_team(team_id) == TeamStatus.SUCCEEDED
    options = file_harness.agents["glm"].options_log[-1]
    assert options.mode == MODE_TEXT and options.directory is None
    assert options.workspace_tools is False
    frozen = json.loads(file_harness.workflow_store.get_run(team_id, 1).plan_json)["steps"][0]
    assert frozen["mode"] == MODE_TEXT and frozen["workspace"] is None


async def test_read_only_member_cannot_request_edit_and_is_not_downgraded(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(
        tmp_path, root=root, console_workspaces=_workspaces(root, modes=("read",))
    )
    harness = TeamHarness(settings, {"glm": FakeAgent("glm", reply="A")})
    try:
        harness.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
        team_id = await create_team(harness, user_id=MEMBER)
        read_receipt = await harness.command(
            f'/team add {team_id} r glm --mode read --dir "{root}" -- 读', user_id=MEMBER
        )
        assert "已添加步骤 r" in read_receipt
        refused = await harness.command(
            f'/team add {team_id} r glm --mode edit --dir "{root}" -- 改', user_id=MEMBER
        )
        assert "目录不可用" in refused
        assert "只有 read" in refused and "edit" in refused
        # 被拒绝的 edit **没有**把已声明的 read 步骤降级/覆盖
        steps = harness.workflow_store.list_steps(team_id)
        assert len(steps) == 1
        assert steps[0].step_id == "r" and steps[0].mode == MODE_READ
        assert steps[0].instruction == "读"
        assert steps[0].workspace == str(canonical(str(root)))
        assert harness.agents["glm"].calls == []
    finally:
        await harness.aclose()


async def test_exec_step_requires_exec_in_acl(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(
        tmp_path, root=root,
        console_workspaces=_workspaces(root, modes=("read", "edit"), admin_modes=("read",)),
    )
    harness = TeamHarness(settings, {"glm": FakeAgent("glm", reply="A")})
    try:
        harness.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
        team_id = await create_team(harness, user_id=MEMBER)
        refused = await harness.command(
            f'/team add {team_id} x glm --mode exec --dir "{root}" -- 跑', user_id=MEMBER
        )
        assert "只有" in refused and "exec" in refused and "read" in refused
        assert harness.workflow_store.count_steps(team_id) == 0
        # 管理员同样受 ACL 约束（admin_modes 只给 read）
        admin_team = await create_team(harness)
        admin_refused = await harness.command(
            f'/team add {admin_team} y glm --mode exec --dir "{root}" -- 跑'
        )
        assert "只有 read" in admin_refused
        assert harness.workflow_store.count_steps(admin_team) == 0
    finally:
        await harness.aclose()


async def test_backend_capability_limits_mode_and_is_not_downgraded(tmp_path):
    """用户 ACL 之外还要满足后端真实能力：不支持的 mode 在 run 前明确拒绝。"""
    root = tmp_path / "proj"
    root.mkdir()
    harness = TeamHarness(workspace_settings(tmp_path, root=root), {
        "limited": FakeAgent("limited", reply="A", broker_modes=frozenset()),
    })
    try:
        team_id = await create_team(harness)
        receipt = await harness.command(
            f'/team add {team_id} r limited --mode read --dir "{root}" -- 读'
        )
        # 草稿可以记录该声明，但必须同时给出能力警告
        assert "已添加步骤 r" in receipt
        assert "capability_not_supported" in receipt and "不会偷偷换后端或降级模式" in receipt
        text = await harness.command(f"/team run {team_id}")
        assert "capability_not_supported" in text and "read" in text
        assert harness.workflow_store.get_workflow(team_id).status == TeamStatus.DRAFT
        assert harness.workflow_store.get_workflow(team_id).round == 0
        assert harness.agents["limited"].calls == []
        plan = await harness.command(f"/team plan {team_id}")
        assert "⛔ capability_not_supported" in plan
    finally:
        await harness.aclose()


async def test_acl_revoked_after_add_is_rechecked_at_run(tmp_path):
    """add 时合法、run 前 ACL 收紧：run 必须拒绝，且不把步骤降级执行。"""
    root = tmp_path / "proj"
    root.mkdir()
    writable = workspace_settings(tmp_path, root=root)
    first = TeamHarness(writable, {"glm": FakeAgent("glm", reply="A")})
    team_id = await create_team(first)
    await first.command(f'/team add {team_id} e glm --mode edit --dir "{root}" -- 改')
    assert first.workflow_store.list_steps(team_id)[0].mode == MODE_EDIT
    first.context_store.close()  # 模拟进程退出：DB 保留

    read_only = workspace_settings(
        tmp_path, root=root, console_workspaces=_workspaces(root, modes=("read",))
    )
    second = TeamHarness(read_only, {"glm": FakeAgent("glm", reply="A")})
    try:
        text = await second.command(f"/team run {team_id}")
        assert "只有 read" in text and "edit" in text
        assert second.workflow_store.get_workflow(team_id).status == TeamStatus.DRAFT
        assert second.workflow_store.get_workflow(team_id).round == 0
        assert second.workflow_store.list_step_runs(team_id, 1) == {}
        assert second.agents["glm"].calls == []
        # plan 也给出同样的拒绝原因（只展示，不授权）
        plan = await second.command(f"/team plan {team_id}")
        assert "⛔" in plan and "只有 read" in plan
    finally:
        await second.aclose()


async def test_dir_dot_requires_binding_and_never_falls_back_to_cwd(file_harness):
    root = file_harness.root
    team_id = await create_team(file_harness)
    refused = await file_harness.command(
        f"/team add {team_id} a glm --mode read --dir . -- 读"
    )
    assert "需要先绑定目录" in refused
    assert file_harness.workflow_store.count_steps(team_id) == 0
    # 绑定之后 --dir . 使用绑定目录（规范路径）
    bound = await file_harness.command(f'/workspace use "{root}"')
    assert "已绑定目录" in bound
    assert "已添加步骤" in await file_harness.command(
        f"/team add {team_id} a glm --mode read --dir . -- 读"
    )
    assert file_harness.workflow_store.list_steps(team_id)[0].workspace == str(
        canonical(str(root))
    )
    # 清除绑定后不能再使用 --dir .
    await file_harness.command("/workspace clear")
    await file_harness.command(f"/team remove {team_id} a")
    assert "需要先绑定目录" in await file_harness.command(
        f"/team add {team_id} a glm --mode read --dir . -- 读"
    )


async def test_directory_conflicts_and_text_mode_are_rejected_by_command(file_harness):
    team_id = await create_team(file_harness)
    root = file_harness.root
    for command, needle in (
        (f'/team add {team_id} a glm --dir "{root}" --find 项目 -- 冲突', "只能二选一"),
        (f'/team add {team_id} a glm --mode text --dir "{root}" -- 文本', "--mode text"),
        (f'/team add {team_id} a glm --mode bogus --dir "{root}" -- 错', "text|read|edit|exec"),
        (f'/team add {team_id} a glm --unknown 1 -- 错', "--unknown"),
    ):
        reply = await file_harness.command(command)
        assert needle in reply, command
    assert file_harness.workflow_store.count_steps(team_id) == 0


# ---------- --find 未选定：计划保持 draft ----------


async def test_find_keeps_step_unresolved_and_run_refuses_everything(file_harness, tmp_path):
    sub = file_harness.root / "子项目"
    sub.mkdir()
    team_id = await create_team(file_harness)
    await file_harness.command(f"/team add {team_id} a glm -- 已经就绪的文本步骤")
    receipt = await file_harness.command(f'/team add {team_id} f glm --find 子项目 -- 找目录')
    assert "已添加步骤 f" in receipt
    assert "/workspace select" in receipt  # 候选必须由本人选择
    steps = {step.step_id: step for step in file_harness.workflow_store.list_steps(team_id)}
    assert steps["f"].find_keyword == "子项目"
    assert steps["f"].workspace is None and steps["f"].mode == MODE_READ
    # 只要有一步未选定，整轮都不启动（计划保持 draft）
    text = await file_harness.command(f"/team run {team_id}")
    assert "目录尚未选择" in text and "f" in text
    workflow = file_harness.workflow_store.get_workflow(team_id)
    assert workflow.status == TeamStatus.DRAFT and workflow.round == 0
    assert file_harness.workflow_store.list_step_runs(team_id, 1) == {}
    assert file_harness.agents["glm"].calls == []
    plan = await file_harness.command(f"/team plan {team_id}")
    assert "目录待选择" in plan and "run 前必须" in plan


async def test_find_selection_resolves_step_and_then_run_starts(file_harness):
    """候选必须由本人 select；选定后写回草稿目录，随后 run 才能启动。"""
    import re

    sub = file_harness.root / "子项目"
    sub.mkdir()
    team_id = await create_team(file_harness)
    await file_harness.command(f"/team add {team_id} f reader --find 子项目 -- 找目录")
    # 未选定：run 拒绝，草稿不变
    assert "目录尚未选择" in await file_harness.command(f"/team run {team_id}")
    codes = [
        match.group(1)
        for text in file_harness.transport.texts()
        if (match := re.search(r"/workspace select ([0-9A-F]{8})", text))
    ]
    assert codes, "回执里应包含目录选择确认码"
    reply = await file_harness.command(f"/workspace select {codes[-1]} 1")
    assert "已选择" in reply and "已冻结目录" in reply
    step = file_harness.workflow_store.list_steps(team_id)[0]
    assert step.workspace == str(canonical(str(sub))) and step.find_keyword is None
    assert step.mode == MODE_READ
    assert "已排入后台执行" in await file_harness.command(f"/team run {team_id}")
    assert await file_harness.wait_team(team_id) == TeamStatus.SUCCEEDED
    frozen = json.loads(file_harness.workflow_store.get_run(team_id, 1).plan_json)["steps"][0]
    assert frozen["workspace"] == str(canonical(str(sub)))


async def test_draft_change_invalidates_pending_selection_confirmation(file_harness):
    """草稿再被修改会使旧确认失效；重新 add 会给出新的确认码。"""
    import re

    sub = file_harness.root / "子项目"
    sub.mkdir()
    team_id = await create_team(file_harness)
    await file_harness.command(f"/team add {team_id} f glm --find 子项目 -- 找目录")

    def codes() -> list[str]:
        return [
            match.group(1)
            for text in file_harness.transport.texts()
            if (match := re.search(r"/workspace select ([0-9A-F]{8})", text))
        ]

    first = codes()[-1]
    await file_harness.command(f"/team add {team_id} other glm -- 无关步骤使草稿变化")
    assert "无效" in await file_harness.command(f"/workspace select {first} 1")
    await file_harness.command(f"/team add {team_id} f glm --find 子项目 -- 重新查找")
    second = codes()[-1]
    assert second != first
    assert "已选择" in await file_harness.command(f"/workspace select {second} 1")
    steps = {step.step_id: step for step in file_harness.workflow_store.list_steps(team_id)}
    assert steps["f"].workspace is not None and steps["f"].find_keyword is None
    assert steps["other"].workspace is None


# ---------- run 前置校验 / 配额 / 限流 ----------


async def test_run_rejects_unknown_dependency_cycle_and_empty_plan(harness):
    team_id = await create_team(harness)
    assert "还没有步骤" in await harness.command(f"/team run {team_id}")
    await harness.command(f"/team add {team_id} a glm --after missing -- 步骤")
    assert "依赖未定义" in await harness.command(f"/team run {team_id}")
    await harness.command(f"/team remove {team_id} a")
    # 自环与普通环
    await harness.command(f"/team add {team_id} b glm --after b -- 自环")
    assert "环" in await harness.command(f"/team run {team_id}")
    await harness.command(f"/team remove {team_id} b")
    await harness.command(f"/team add {team_id} x glm --after y -- 环1")
    await harness.command(f"/team add {team_id} y glm --after x -- 环2")
    assert "环" in await harness.command(f"/team run {team_id}")
    assert harness.agents["glm"].calls == []
    # 完整替换可打断环：先清掉 x 的依赖，再删 y
    await harness.command(f"/team add {team_id} x glm -- 无依赖")
    await harness.command(f"/team remove {team_id} y")
    await harness.command(f"/team add {team_id} y glm --after missing -- 未定义")
    plan = await harness.command(f"/team plan {team_id}")
    assert "未定义依赖" in plan


async def test_run_refuses_undefined_dependency(harness):
    """形态合法但不存在的依赖，必须在 run 前被拒绝且不调用模型。"""
    team_id = await create_team(harness)
    await harness.command(f"/team add {team_id} a glm --after missing -- 步骤")
    text = await harness.command(f"/team run {team_id}")
    assert "依赖未定义" in text and "missing" in text
    assert harness.agents["glm"].calls == []
    # 形态非法的依赖在解析期就被拒绝，不会进入草稿
    refused = await harness.command(f"/team add {team_id} b glm --after BAD -- 步骤")
    assert "--after" in refused and "BAD" in refused


async def test_step_limit(harness):
    team_id = await create_team(harness)
    for index in range(8):
        assert "已添加步骤" in await harness.command(
            f"/team add {team_id} s{index} glm -- 步骤{index}"
        )
    assert "上限" in await harness.command(f"/team add {team_id} s9 glm -- 超限")
    # 同 ID 替换不受上限限制
    assert "已完整替换步骤" in await harness.command(f"/team add {team_id} s0 glm -- 替换")


async def test_quota_per_user_and_global(tmp_path):
    settings = make_team_settings(tmp_path, team_max_active_per_user=1,
                                  team_max_active_global=1)
    agents = {"glm": FakeAgent("glm", delay=0.2, reply="A")}
    harness = TeamHarness(settings, agents)
    try:
        first = await create_team(harness)
        await harness.command(f"/team add {first} a glm -- 步骤")
        assert "已排入后台执行" in await harness.command(f"/team run {first}")
        second = await create_team(harness)
        await harness.command(f"/team add {second} a glm -- 步骤")
        text = await harness.command(f"/team run {second}")
        assert "活动任务" in text and "上限" in text
        assert harness.workflow_store.get_workflow(second).status == TeamStatus.DRAFT
        await harness.wait_team(first)
    finally:
        await harness.aclose()


async def test_run_rate_limited(tmp_path):
    # 限流对「发起 run」与「步骤执行（TaskService）」共用同一个窗口：
    # 1 次成功的单步 run 恰好消耗 2 个令牌。
    settings = make_team_settings(tmp_path, rate_limit_per_minute=2)
    agents = {"glm": FakeAgent("glm", reply="A")}
    harness = TeamHarness(settings, agents)
    try:
        first = await create_team(harness)
        await harness.command(f"/team add {first} a glm -- 步骤")
        assert "已排入后台执行" in await harness.command(f"/team run {first}")
        assert await harness.wait_team(first) == TeamStatus.SUCCEEDED
        # 限流窗口未过：第二次 run 被拒绝，且不产生新轮次
        second = await create_team(harness)
        await harness.command(f"/team add {second} a glm -- 步骤")
        text = await harness.command(f"/team run {second}")
        assert "频繁" in text
        assert harness.workflow_store.get_workflow(second).status == TeamStatus.DRAFT
        assert harness.workflow_store.get_workflow(second).round == 0
    finally:
        await harness.aclose()


async def test_step_execution_rate_limit_fails_step_not_team_silently(tmp_path):
    """步骤执行被限流时如实记为失败，不重试也不静默成功。"""
    settings = make_team_settings(tmp_path, rate_limit_per_minute=1)
    agents = {"glm": FakeAgent("glm", reply="A")}
    harness = TeamHarness(settings, agents)
    try:
        team_id = await create_team(harness)
        await harness.command(f"/team add {team_id} a glm -- 步骤")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id) == TeamStatus.FAILED
        record = harness.workflow_store.get_step_run(team_id, 1, "a")
        assert record.status == StepStatus.FAILED and record.error == "rate_limited"
        result = await harness.command(f"/team result {team_id}")
        assert "频繁" in result and "失败" in result
    finally:
        await harness.aclose()


# ---------- retry / cancel ----------


async def test_partial_success_then_retry_and_cancel_semantics(harness):
    team_id = await create_team(harness)
    await harness.command(f"/team add {team_id} a glm -- 成功")
    await harness.command(f"/team add {team_id} b deepseek --after a -- 失败")
    harness.agents["deepseek"]._error = "network"
    await harness.command(f"/team run {team_id}")
    assert await harness.wait_team(team_id) == TeamStatus.FAILED
    # 成功结果可查询，失败步骤给出错误类别
    result = await harness.command(f"/team result {team_id}")
    assert "A" in result and "网络错误" in result
    # 已失败 → retry 允许
    assert "第 2 轮" in await harness.command(f"/team retry {team_id}")
    harness.agents["deepseek"]._error = None
    assert await harness.wait_team(team_id) == TeamStatus.SUCCEEDED
    # 成功任务不能 retry
    assert "仅失败或中断" in await harness.command(f"/team retry {team_id}")


async def test_cancel_is_idempotent_and_draft_cancel(harness):
    draft = await create_team(harness)
    assert "已取消草稿" in await harness.command(f"/team cancel {draft}")
    assert "无需重复取消" in await harness.command(f"/team cancel {draft}")
    assert "不支持 retry" in await harness.command(f"/team retry {draft}")


async def test_member_text_step_is_text_only(harness):
    team_id = await create_team(harness, user_id=MEMBER)
    assert "已添加步骤" in await harness.command(
        f"/team add {team_id} z zcode -- 文本任务", user_id=MEMBER
    )
    await harness.command(f"/team run {team_id}", user_id=MEMBER)
    assert await harness.wait_team(team_id) == TeamStatus.SUCCEEDED
    assert harness.agents["zcode"].options_log
    for options in harness.agents["zcode"].options_log:
        assert options.mode == MODE_TEXT
        assert options.directory is None
        assert options.workspace_tools is False


async def test_status_lists_recent_and_other_users_isolated(harness):
    team_id = await create_team(harness)
    listing = await harness.command("/team status")
    assert team_id in listing
    other = await harness.command("/team status", user_id=MEMBER)
    assert team_id not in other
    assert "还没有团队任务" in other


async def test_revoke_access_cancels_running_team(tmp_path):
    import asyncio

    entered = asyncio.Event()
    released = asyncio.Event()

    async def hold(agent, messages) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm", hook=hold)}
    harness = TeamHarness(settings, agents)
    try:
        harness.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, True, ALLOWED_USER)
        team_id = await create_team(harness, user_id=MEMBER)
        await harness.command(f"/team add {team_id} a glm -- 等待", user_id=MEMBER)
        await harness.command(f"/team run {team_id}", user_id=MEMBER)
        await asyncio.wait_for(entered.wait(), timeout=3)
        # 撤销授权（正常由 /access remove 触发）
        harness.context_store.set_whitelist(ALLOWED_GROUP, MEMBER, False, ALLOWED_USER)
        cancelled = await harness.team_service.revoke_access(ALLOWED_GROUP, MEMBER)
        assert cancelled == 1
        await asyncio.wait_for(released.wait(), timeout=1)
        assert harness.workflow_store.get_workflow(team_id).status == TeamStatus.CANCELLED
        assert harness.runner.active_teams == ()
    finally:
        await harness.aclose()
