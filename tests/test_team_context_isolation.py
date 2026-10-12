"""A16：team 上下文隔离——只传目标/本步/直接依赖；不读写普通聊天历史。"""

from __future__ import annotations

import json
from pathlib import Path

from app.workflows.models import TeamStatus
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    workspace_settings,
)


def _payload(agent: FakeAgent) -> dict:
    """取出本步的结构化输入（去掉"不可信数据"标注行）。"""
    content = agent.calls[-1][-1].content
    if content.startswith("以下为"):
        content = content.split("\n", 1)[1]
    return json.loads(content)


async def test_step_receives_only_goal_instruction_and_direct_upstream(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    one = FakeAgent("deepseek", reply="第一步结论")
    two = FakeAgent("zcode", reply="第二步结论")
    three = FakeAgent("gpt", reply="第三步结论")
    harness = ConsoleHarness(settings, {"deepseek": one, "zcode": two, "gpt": three})
    try:
        team = harness.create_team("隔离检查")
        harness.add_step(team, "a", "deepseek", "任务A")
        harness.add_step(team, "b", "zcode", "任务B")
        harness.add_step(team, "c", "gpt", "任务C", after=("a",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED

        step_c = _payload(three)
        assert step_c["team_goal"] == "隔离检查"
        assert step_c["step_instruction"] == "任务C"
        upstream_ids = [item["step_id"] for item in step_c["upstream"]]
        assert upstream_ids == ["a"]           # 只有直接依赖
        assert "b" not in upstream_ids          # 兄弟步骤不可见
        assert "第二步结论" not in json.dumps(step_c, ensure_ascii=False)
        # capability 字段如实反映本步权限
        assert step_c["capability"] == {"mode": "text", "directory": None, "tools": False}
    finally:
        await harness.aclose()


async def test_chat_history_is_not_passed_to_team_steps(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    agent = FakeAgent("deepseek", reply="结论")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        # 先产生一段普通聊天上下文
        await harness.command("/task deepseek -- 我的名字是小明")
        assert harness.context_store.recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek", max_turns=5
        )
        agent.calls.clear()
        team = harness.create_team("团队")
        harness.add_step(team, "s1", "deepseek", "只依据本步指令回答")
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        # 团队步骤的消息里没有普通聊天历史
        rendered = json.dumps([[m.role, m.content] for m in agent.calls[-1]], ensure_ascii=False)
        assert "小明的" not in rendered and "我的名字是小明" not in rendered
    finally:
        await harness.aclose()


async def test_successful_team_step_does_not_write_chat_context(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    agent = FakeAgent("deepseek", reply="团队结论")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        before = harness.context_store.recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek", max_turns=50
        )
        team = harness.create_team("不写历史")
        harness.add_step(team, "s1", "deepseek", "做")
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        after = harness.context_store.recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek", max_turns=50
        )
        assert after == before == []
    finally:
        await harness.aclose()


async def test_team_steps_do_not_see_other_teams_or_users(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    settings.qq_allowed_user_ids = [ALLOWED_USER, 101]
    other = FakeAgent("zcode", reply="别人的团队结论")
    mine = FakeAgent("deepseek", reply="我的结论")
    harness = ConsoleHarness(settings, {"deepseek": mine, "zcode": other})
    try:
        theirs = harness.create_team("别人的目标", user_id=101)
        harness.add_step(theirs, "s", "zcode", "别人的指令")
        await harness.command(f"/team run {theirs}", user_id=101)
        await harness.wait_team(theirs)

        team = harness.create_team("我的目标")
        harness.add_step(team, "s", "deepseek", "我的指令")
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        payload = _payload(mine)
        assert payload["team_goal"] == "我的目标"
        assert "别人的" not in json.dumps(payload, ensure_ascii=False)
        # 别人的任务不可见
        status = await harness.command(f"/team status {theirs}")
        assert "不存在" in status
    finally:
        await harness.aclose()


async def test_upstream_answer_is_marked_as_untrusted_data(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    first = FakeAgent("deepseek", reply="上游正文")
    second = FakeAgent("zcode", reply="下游结论")
    harness = ConsoleHarness(settings, {"deepseek": first, "zcode": second})
    try:
        team = harness.create_team("标注")
        harness.add_step(team, "a", "deepseek", "一")
        harness.add_step(team, "b", "zcode", "二", after=("a",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        system_prompt = second.calls[-1][0].content
        user_message = second.calls[-1][-1].content
        assert "不是指令" in system_prompt
        assert "不可信数据" in user_message
        assert "上游" in user_message
    finally:
        await harness.aclose()


def test_step_payload_shape_is_stable() -> None:
    from app.workflows.executor import StepContext, build_step_payload
    from app.workflows.models import FrozenChoice, FrozenStep, UpstreamResult

    step = FrozenStep(
        step_id="s1", seq=0, agent="deepseek", instruction="做",
        depends_on=("up",),
        choice=FrozenChoice(model="m", effort=None, effort_set=False),
        mode="read", backend="b", workspace="/tmp/x",
    )
    context = StepContext(
        team_id="t1", round=1, group_id=ALLOWED_GROUP, owner_user_id=ALLOWED_USER,
        goal="目标", step=step,
        upstream=(UpstreamResult(step_id="up", agent="zcode", answer="上游"),),
    )
    payload = json.loads(build_step_payload(context))
    assert set(payload) == {
        "team_goal", "step_id", "step_instruction", "capability", "upstream"
    }
    assert payload["capability"]["directory"] == "/tmp/x"
    assert payload["capability"]["tools"] is True
    assert payload["upstream"][0]["answer"] == "上游"
