"""`/team` 路由与全链路集成测试：Dispatcher、持久化重启、lifespan 关闭顺序。

全部使用 FakeAgent + RecordingTransport + 临时 DB：不向真实 QQ 发消息，
不调用真实模型，不触碰生产数据库。
"""

from __future__ import annotations

import httpx

from app.main import create_app
from app.workflows.models import (
    FrozenChoice,
    FrozenStep,
    PlanSnapshot,
    StepStatus,
    TeamStatus,
)
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    FakeAgent,
    RecordingTransport,
    TeamHarness,
    build_dispatcher,
    make_message,
    make_settings,
    make_team_settings,
)

TEAM = "t7a91c20d"


def snapshot(team_id: str, *steps: tuple[str, str]) -> PlanSnapshot:
    return PlanSnapshot(
        team_id=team_id, goal="目标",
        steps=tuple(
            FrozenStep(step_id=step_id, seq=index, agent=agent, instruction="步骤",
                       depends_on=(), choice=FrozenChoice(model=f"fake-{agent}"))
            for index, (step_id, agent) in enumerate(steps)
        ),
    )


async def test_team_route_and_help(tmp_path):
    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm", reply="A")}
    harness = TeamHarness(settings, agents)
    try:
        # 未知子命令有明确回执，不调用模型
        reply = await harness.command("/team frobnicate")
        assert "未知的 /team 子命令" in reply
        assert harness.agents["glm"].calls == []
        # /help 与 /help team 已包含 /team 说明与公共能力选项
        help_text = await harness.command("/help")
        assert "/team create" in help_text
        family = await harness.command("/help team")
        for option in ("--mode", "--dir", "--find", "--after"):
            assert option in family
        # 全链路
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        assert "已添加步骤" in await harness.command(f"/team add {team_id} a glm -- 第一步")
        plan = await harness.command(f"/team plan {team_id}")
        assert "可并行层次" in plan
        assert "权限 文本（无工具）" in plan  # 未声明能力 = 纯文本
        assert "已排入后台执行" in await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id) == TeamStatus.SUCCEEDED
        status = await harness.command(f"/team status {team_id}")
        assert "已成功" in status and "a → glm" in status
        assert "A" in await harness.command(f"/team result {team_id} a")
        # 只经过记录型传输，没有任何真实外发通道
        assert isinstance(harness.transport, RecordingTransport)
        assert harness.transport.sends and all(
            group == ALLOWED_GROUP for group, _ in harness.transport.sends
        )
    finally:
        await harness.aclose()


async def test_team_disabled_reply(tmp_path):
    settings = make_team_settings(tmp_path, team_enabled=False)
    agents = {"glm": FakeAgent("glm")}
    harness = TeamHarness(settings, agents)
    try:
        reply = await harness.command("/team create 目标")
        assert "未启用" in reply
        assert harness.agents["glm"].calls == []
    finally:
        await harness.aclose()


async def test_team_route_without_handler_reports_disabled(tmp_path):
    """没有注入 team handler 时 /team 只回复未启用，不执行任何操作。"""
    settings = make_team_settings(tmp_path)
    agent = FakeAgent("glm")
    dispatcher, transport = build_dispatcher(settings, {"glm": agent})
    dispatcher.team = None  # 模拟未装配协作处理器
    await dispatcher.handle_message(make_message("/team create 目标"))
    assert "未启用" in transport.texts()[-1]
    assert agent.calls == []


async def test_unauthorized_user_cannot_reach_team(tmp_path):
    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    harness = TeamHarness(settings, agents)
    try:
        reply = await harness.command("/team create 目标", user_id=987654)
        assert "未授权" in reply
        assert harness.workflow_store.list_recent_teams(
            group_id=ALLOWED_GROUP, owner_user_id=987654
        ) == []
    finally:
        await harness.aclose()


async def test_restart_marks_interrupted_and_manual_retry_recovers(tmp_path):
    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm", reply="A-成功")}
    first = TeamHarness(settings, agents)
    team_id = first.create_team("目标")
    first.add_step(team_id, "a", "glm")
    assert first.workflow_store.freeze_run(team_id, snapshot(team_id, ("a", "glm"))) == 1
    first.context_store.close()  # 模拟进程退出：DB 保留，任务停在 queued

    # 重启：新连接 + 启动恢复标记 interrupted，不自动重新调用模型
    second = TeamHarness(settings, {"glm": FakeAgent("glm", reply="A-成功")})
    try:
        assert second.workflow_store.mark_interrupted_on_startup() == 1
        assert second.workflow_store.get_workflow(team_id).status == TeamStatus.INTERRUPTED
        assert second.workflow_store.get_step_run(
            team_id, 1, "a"
        ).status == StepStatus.INTERRUPTED
        assert second.agents["glm"].calls == []  # 未自动重跑
        # 计划与轮次记录保留，可显式 retry
        assert second.workflow_store.get_run(team_id, 1) is not None
        text = await second.command(f"/team retry {team_id}")
        assert "第 2 轮" in text
        assert await second.wait_team(team_id) == TeamStatus.SUCCEEDED
        assert len(second.agents["glm"].calls) == 1
    finally:
        await second.aclose()


async def test_lifespan_marks_interrupted_and_stops_runner(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    from app.storage import ContextStore
    from app.workflows.store import WorkflowStore

    store = ContextStore(settings.database_path)
    workflow_store = WorkflowStore(store.connection)
    workflow_store.migrate()
    draft = workflow_store.create_draft(
        group_id=ALLOWED_GROUP, owner_user_id=ALLOWED_USER, goal="重启目标"
    )
    workflow_store.replace_step(
        team_id=draft.team_id, step_id="a", agent="glm", instruction="步骤",
        depends_on=(), model=None, effort=None, effort_set=False,
    )
    workflow_store.freeze_run(draft.team_id, snapshot(draft.team_id, ("a", "glm")))
    store.close()

    created: list[RecordingTransport] = []

    def fake_build_transport(settings, on_event):
        transport = RecordingTransport()
        created.append(transport)
        return transport

    import app.main as main_module

    monkeypatch.setattr(main_module, "build_transport", fake_build_transport)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = (await client.get("/status")).json()
        assert payload["status"] == "ok"
        assert payload["limits"]["team_enabled"] is True
        assert payload["limits"]["team_max_parallel_steps"] == 2
        assert app.state.workflows.get_workflow(draft.team_id).status == TeamStatus.INTERRUPTED
        assert app.state.workflow_runner.active_teams == ()
    # lifespan 退出后 runner 已停止且无遗留任务
    assert app.state.workflow_runner.active_teams == ()
