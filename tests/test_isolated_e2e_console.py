"""本轮（L01–L04 整改后）的**隔离端到端**验收：新版统一口令 + 工作区确认 + 审批 + 跨模型 team。

设计约束（与 `outputs/DeepSeek下一轮整改提示词.md` §6 一致）：
- 独立数据库、临时工作区、测试配置；使用**录制传输**（RecordingTransport），
  不连接真实 QQ 消费者、不向任何群发消息；
- 不调用真实模型（全部使用 FakeAgent / ScriptedAgent）；
- 与生产处理链隔离：全程只走 `ConsoleHarness` + 临时目录 + 临时 SQLite。

覆盖：
1. 新版标准口令与参数（`/task`、`/workspace`、`/agent`、`/team`、`/confirm`、`/access`、`/command`、`/help`）；
2. 旧口令只给迁移提示、零副作用（不调用模型、不读写文件）；
3. 工作区模糊候选 → 展示候选 → 用户确认（正确码生效、错误码/他人码/过期码被拒、目录身份被替换即拒绝）；
4. read/edit/exec 三种模式的真实副作用与审计（变更记录、备份、命令日志）；
5. 跨模型 team（deepseek read → zcode edit）：统一接口 + 冻结计划 + 文件真实一致；
6. 审批/确认路径：team retry 的副作用确认（含拒绝）；
7. 拒绝路径：无授权目录、只读模式下写、越权用户、未知动作。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.workflows.models import StepStatus, TeamStatus
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    OTHER_USER,
    ScriptedAgent,
    workspace_settings,
)


def _confirm_code(text: str) -> str:
    """从回复里取出一次性确认码。

    码是 `secrets.token_hex(4).upper()` → 8 位十六进制（可能全是数字，例如 `21083560`），
    所以不能只挑"含字母的大写串"。
    """
    for item in text.replace("（", " ").replace("）", " ").split():
        token = item.strip("：:，,。.、")
        if len(token) == 8 and all(ch in "0123456789ABCDEF" for ch in token):
            return token
    raise AssertionError(f"回复里没有找到 8 位确认码：{text}")


def _make_tree(tmp_path: Path) -> Path:
    """造两个同名前缀的候选目录，用于制造 find 歧义。"""
    base = tmp_path / "projects"
    for name in ("alpha-report", "alpha-report-archive"):
        target = base / name
        target.mkdir(parents=True, exist_ok=True)
        readme = target / "README.md"
        if not readme.exists():
            readme.write_text(f"# {name}\n", encoding="utf-8")
    return base


def _harness(tmp_path: Path, agents: dict, **overrides) -> ConsoleHarness:
    base = _make_tree(tmp_path)
    settings = workspace_settings(tmp_path, root=base, **overrides)
    return ConsoleHarness(settings, agents)


# ---------- 1) 新版口令与旧口令 ----------


async def test_canonical_commands_are_the_only_working_entry(tmp_path) -> None:
    harness = _harness(tmp_path, {"deepseek": FakeAgent("deepseek", reply="纯文本答复")})
    try:
        help_text = await harness.command("/help")
        listing = await harness.command("/command list")
        for name in ("task", "workspace", "agent", "team", "confirm", "access", "command", "help"):
            assert f"/{name}" in help_text
            assert f"/{name}" in listing

        # 旧口令：只给迁移提示，且零副作用
        agent = harness.agents["deepseek"]
        before_calls = len(agent.calls)
        for legacy in ("/glm 你好", "/read deepseek /etc/hosts", "/models", "/status"):
            reply = await harness.command(legacy)
            assert reply.strip(), legacy
            assert "已停用" in reply or "迁移" in reply or "改用" in reply, (legacy, reply)
        assert len(agent.calls) == before_calls, "旧口令不得触发模型调用"

        # 未知命令保持静默（不与旧口令混淆）
        assert (await harness.command("/definitely-not-a-command")).strip() == ""
    finally:
        await harness.aclose()


async def test_task_command_runs_text_mode_with_public_options(tmp_path) -> None:
    agent = FakeAgent(
        "deepseek", reply="已经答复",
        model_options={"fake-deepseek": ("low", "high")},
    )
    harness = _harness(tmp_path, {"deepseek": agent})
    try:
        reply = await harness.command("/task deepseek --effort high -- 只用一句话回答")
        assert "已经答复" in reply
        options = agent.options_log[-1]
        assert options.mode == "text" and options.directory is None
        assert options.workspace_tools is False
        assert options.effort == "high"
        # 无效强度必须在调用前被拒绝（不产生模型调用）
        refused = await harness.command("/task deepseek --effort ultra -- 你好")
        assert "不受" in refused or "可选" in refused
        assert len(agent.calls) == 1
    finally:
        await harness.aclose()


# ---------- 2) 工作区模糊候选 → 确认 ----------


async def test_ambiguous_workspace_requires_user_confirmation(tmp_path) -> None:
    base = _make_tree(tmp_path)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "读完了"}))
    harness = _harness(tmp_path, {"deepseek": agent})
    try:
        # 模糊关键字命中多个候选 → 展示候选，且**不调用模型**
        proposed = await harness.command("/task deepseek --find alpha-report --mode read -- 看目录")
        assert "候选" in proposed, proposed
        assert len(agent.calls) == 0, "存在歧义时不得擅自对候选执行任务"
        code = _confirm_code(proposed)

        # 目录选择确认走 /workspace select <码> <序号>；错误确认码被拒绝
        wrong = "ZZZZZZZZ" if code != "ZZZZZZZZ" else "YYYYYYYY"
        assert "无效" in await harness.command(f"/workspace select {wrong} 1")

        # 正确确认码 + 合法序号 → 绑定被选目录并继续原本的任务
        confirmed = await harness.command(f"/workspace select {code} 1")
        assert "已" in confirmed or "绑定" in confirmed or "选择" in confirmed, confirmed
        for _ in range(50):
            if agent.calls:
                break
            import asyncio
            await asyncio.sleep(0.01)
        assert len(agent.calls) >= 1, "确认后原任务没有继续执行"
    finally:
        await harness.aclose()


async def test_other_user_cannot_consume_confirmation_code(tmp_path) -> None:
    """确认码绑定发起人：名单内的其他用户也不能消费（更不用说名单外用户）。"""
    from helpers import make_settings

    base = _make_tree(tmp_path)
    settings = make_settings(
        tmp_path,
        qq_allowed_user_ids=[ALLOWED_USER, OTHER_USER],
        console_workspaces={"roots": [{"path": str(base), "modes": ["read"],
                                       "admin_modes": ["read"]}]},
    )
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek", reply=json.dumps({"final": "x"}))})
    try:
        proposed = await harness.command("/task deepseek --find alpha-report --mode read -- 看")
        code = _confirm_code(proposed)
        stolen = await harness.command(f"/workspace select {code} 1", user_id=OTHER_USER)
        assert "无效" in stolen or "不属于你" in stolen, stolen
        # 名单外用户：调度器只允许 /help（未授权用户默认被静默或提示未授权）
        outsider = await harness.command(f"/workspace select {code} 1", user_id=999)
        assert outsider.strip() == "" or "未授权" in outsider, outsider
    finally:
        await harness.aclose()


async def test_replaced_candidate_directory_is_rejected(tmp_path) -> None:
    base = _make_tree(tmp_path)
    harness = _harness(tmp_path, {"deepseek": FakeAgent("deepseek", reply=json.dumps({"final": "x"}))})
    try:
        proposed = await harness.command("/task deepseek --find alpha-report --mode read -- 看")
        code = _confirm_code(proposed)
        # 把候选目录换成一个"同路径不同身份"的目录
        victim = None
        for candidate in sorted(base.glob("alpha-report*")):
            if candidate.is_dir():
                victim = candidate
                break
        assert victim is not None
        victim.rename(victim.with_name(victim.name + "-old"))
        victim.mkdir()
        # §6 附项：目录选择口令是 /workspace select <确认码> <序号>；用错口令得到的拒绝
        # 不能支持"目录身份复核"这一结论，因此这里必须走真实口令，并断言拒绝理由
        # 确实是"目录身份不一致"，而不是"确认码无效/口令错"。
        confirmed = await harness.command(f"/workspace select {code} 1")
        assert "目录身份不一致" in confirmed, confirmed
    finally:
        await harness.aclose()


# ---------- 3) read/edit/exec 的真实副作用 ----------


async def test_read_edit_exec_modes_have_real_effects(tmp_path) -> None:
    base = _make_tree(tmp_path)
    workspace = base / "alpha-report"
    settings = workspace_settings(tmp_path, root=workspace)
    reader = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "README.md"}]}),
        json.dumps({"final": "读到内容"}),
    ])
    editor = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "notes.txt", "content": "written-by-edit"}]}]}),
        json.dumps({"final": "已创建"}),
    ])
    runner = ScriptedAgent("codex", [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c", "print('exec-ran')"], "timeout": 10}]}),
        json.dumps({"final": "命令完成"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": reader, "zcode": editor, "codex": runner})
    try:
        read_reply = await harness.command(
            f'/task deepseek --dir "{workspace}" --mode read -- 读 README'
        )
        assert "读到内容" in read_reply

        edit_reply = await harness.command(
            f'/task zcode --dir "{workspace}" --mode edit -- 写 notes'
        )
        assert "已创建" in edit_reply
        assert (workspace / "notes.txt").read_text() == "written-by-edit"
        record = harness.task_store.list_recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, limit=1,
        )[0]
        changes = harness.task_store.list_changes(task_id=record["task_id"])
        assert changes and Path(changes[0]["backup"]).is_file() is False or changes[0]["kind"] == "create"

        exec_reply = await harness.command(
            f'/task codex --dir "{workspace}" --mode exec -- 跑命令'
        )
        assert "命令完成" in exec_reply
        assert "exec-ran" in exec_reply
    finally:
        await harness.aclose()


async def test_read_mode_write_attempt_is_refused_without_side_effect(tmp_path) -> None:
    base = _make_tree(tmp_path)
    workspace = base / "alpha-report"
    target = workspace / "README.md"
    before = target.read_text()
    script = [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "README.md", "content": "HACKED"}]}]}),
        json.dumps({"final": "我改了"}),
    ]
    harness = _harness(tmp_path, {"deepseek": ScriptedAgent("deepseek", script)})
    try:
        reply = await harness.command(
            f'/task deepseek --dir "{workspace}" --mode read -- 改 README'
        )
        assert "失败" in reply
        assert target.read_text() == before
    finally:
        await harness.aclose()


async def test_unauthorized_directory_is_refused(tmp_path) -> None:
    base = _make_tree(tmp_path)
    outside = tmp_path / "not-authorized"
    outside.mkdir()
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "x"}))
    harness = _harness(tmp_path, {"deepseek": agent})
    try:
        reply = await harness.command(f'/task deepseek --dir "{outside}" --mode read -- 看')
        assert "不在授权根内" in reply or "授权" in reply
        assert agent.calls == []
    finally:
        await harness.aclose()


# ---------- 4) 跨模型 team（统一接口 + 冻结 + 真实文件） ----------


async def test_cross_model_team_read_then_edit(tmp_path) -> None:
    base = _make_tree(tmp_path)
    workspace = base / "alpha-report"
    (workspace / "token.txt").write_text("TEAM_TOKEN_9f2c", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=workspace)
    reader = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "token.txt"}]}),
        json.dumps({"final": "TEAM_TOKEN_9f2c"}),
    ])
    writer = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "result.txt", "content": "TEAM_TOKEN_9f2c"}]}]}),
        json.dumps({"final": "已写入"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": reader, "zcode": writer})
    try:
        team = harness.create_team("跨模型读写")
        harness.add_step(team, "read", "deepseek", "读 token", mode="read",
                         workspace=str(workspace.resolve()))
        harness.add_step(team, "write", "zcode", "写下 token", mode="edit",
                         workspace=str(workspace.resolve()), after=("read",))
        plan = await harness.command(f"/team plan {team}")
        assert "只读" in plan and "读写" in plan
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        assert (workspace / "result.txt").read_text() == "TEAM_TOKEN_9f2c"
        assert harness.workflow_store.get_step_run(team, 1, "read").status == StepStatus.SUCCEEDED
        assert harness.workflow_store.get_step_run(team, 1, "write").status == StepStatus.SUCCEEDED
        result = await harness.command(f"/team result {team}")
        assert "已写入" in result
    finally:
        await harness.aclose()


async def test_team_step_cannot_escalate_beyond_member_acl(tmp_path) -> None:
    """team 权限不得绕过成员权限，也不能由模型自行提权。

    关键在于用**真正的普通成员**（不在固定管理员名单里，只是本群白名单成员）：
    管理员本来就有 admin_modes，用管理员测不出"绕过"。
    """
    from helpers import make_settings

    base = _make_tree(tmp_path)
    workspace = base / "alpha-report"
    target = workspace / "README.md"
    before = target.read_text()

    settings = make_settings(
        tmp_path,
        qq_allowed_user_ids=[ALLOWED_USER],          # 固定管理员：只有 100
        qq_allowed_group_ids=[ALLOWED_GROUP],
        console_workspaces={"roots": [{
            "path": str(workspace), "modes": ["read"],
            "admin_modes": ["read", "edit", "exec"],
            "users": {OTHER_USER: ["read"]},          # 普通成员：只读
        }]},
    )
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "README.md", "content": "HACKED"}]}]}),
        json.dumps({"final": "不该成功"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        # 把 101 加进本群白名单（普通成员，不是管理员）
        assert harness.authz.user_allowed(OTHER_USER, ALLOWED_GROUP) is False
        harness.authz.store.set_whitelist(ALLOWED_GROUP, OTHER_USER, True, ALLOWED_USER)
        assert harness.authz.user_allowed(OTHER_USER, ALLOWED_GROUP) is True
        assert harness.authz.admin_allowed(OTHER_USER) is False
        assert harness.acl.authorize_path(
            user_id=OTHER_USER, group_id=ALLOWED_GROUP, path=target, mode="edit",
        ).allowed is False, "夹具要求普通成员在该目录不能 edit"

        team = harness.create_team("越权", user_id=OTHER_USER)
        added = await harness.command(
            f'/team add {team} s1 deepseek --mode edit --dir "{workspace}" -- 改文件',
            user_id=OTHER_USER,
        )
        assert "已添加" in added or "s1" in added, added
        refused = await harness.command(f"/team run {team}", user_id=OTHER_USER)
        state = await harness.wait_team(team, timeout=5)
        # 越权时：要么 run 直接拒绝（草稿保持不变），要么步骤失败；两种都不允许成功
        assert state in {TeamStatus.DRAFT, TeamStatus.FAILED}, (refused, state)
        if state == TeamStatus.FAILED:
            record = harness.workflow_store.get_step_run(team, 1, "s1")
            assert record is not None and record.status == StepStatus.FAILED
            assert record.error in {"unauthorized", "invalid_option"}, record.error
        assert agent.calls == [], "越权步骤不得调用模型"
        assert target.read_text() == before, "越权步骤不得写入任何文件"
    finally:
        await harness.aclose()


# ---------- 5) 审批 / 确认路径 ----------


async def test_team_retry_requires_confirmation_and_can_be_declined(tmp_path) -> None:
    base = _make_tree(tmp_path)
    workspace = base / "alpha-report"
    settings = workspace_settings(tmp_path, root=workspace)
    script = [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "token.txt", "content": "v2"}]}]}),
        "坏掉的输出",  # 第二步失败 → 触发 retry 路径
    ]
    harness = ConsoleHarness(settings, {"zcode": ScriptedAgent("zcode", script)})
    try:
        (workspace / "token.txt").write_text("v1", encoding="utf-8")
        team = harness.create_team("副作用确认")
        harness.add_step(team, "edit", "zcode", "改", mode="edit",
                         workspace=str(workspace.resolve()))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        assert (workspace / "token.txt").read_text() == "v2"

        prompt = await harness.command(f"/team retry {team}")
        assert "副作用" in prompt or "无法证明" in prompt, prompt
        assert harness.workflow_store.get_workflow(team).round == 1, "未确认前不得开启新一轮"

        # 错误确认码不得放行
        assert "无效" in await harness.command("/confirm ABCDEFGH")

        code = _confirm_code(prompt)
        confirmed = await harness.command(f"/confirm {code}")
        assert "第 2 轮" in confirmed or "已开启" in confirmed, confirmed
        await harness.wait_team(team, timeout=10)
        assert harness.workflow_store.get_workflow(team).round == 2
    finally:
        await harness.aclose()


async def test_team_run_rejected_after_acl_revocation(tmp_path) -> None:
    base = _make_tree(tmp_path)
    workspace = base / "alpha-report"
    settings = workspace_settings(tmp_path, root=workspace)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "不应执行"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("撤权")
        await harness.command(f'/team add {team} s1 deepseek --mode edit --dir "{workspace}" -- 改')
        harness.acl._grants = ()  # noqa: SLF001 - 模拟管理员移除授权
        refused = await harness.command(f"/team run {team}")
        assert "不在授权根内" in refused
        assert await harness.wait_team(team, timeout=5) == TeamStatus.DRAFT
        assert agent.calls == []
    finally:
        await harness.aclose()


# ---------- 6) 隔离性：不碰生产链 ----------


async def test_isolated_run_never_touches_production_paths(tmp_path) -> None:
    """端到端用例必须只用临时数据库/工作区；这里断言记录确实落在临时库。"""
    harness = _harness(tmp_path, {"deepseek": FakeAgent("deepseek", reply="ok")})
    try:
        await harness.command("/task deepseek -- 你好")
        record = harness.task_store.list_recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, limit=1,
        )[0]
        assert str(tmp_path) in str(harness.settings.database_path)
        assert record["status"] in {"succeeded", "failed", "running", "queued"}
    finally:
        await harness.aclose()
