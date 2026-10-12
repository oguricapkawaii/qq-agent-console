"""生产装配路径的QQ协作验收：真实 handler/DB/ACL/目录锁，假模型和传输。"""
import asyncio
import json
import re

from app.agents.registry import AgentRegistry
from app.main import create_app
from helpers import (
    FakeAgent,
    RecordingTransport,
    ScriptedAgent,
    make_message,
    make_settings,
    workspace_settings,
)


async def until(check):
    async with asyncio.timeout(3):
        while not check():
            await asyncio.sleep(0.005)


def wire(monkeypatch, agents):
    """本文件局部助手：用假 Agent 与记录型传输装配生产 app。"""
    import app.main as main

    registry = AgentRegistry(agents)
    transport = RecordingTransport()
    monkeypatch.setattr(main.AgentRegistry, "from_settings", lambda settings: registry)
    monkeypatch.setattr(main, "build_transport", lambda settings, on_event: transport)
    return transport


async def test_qq_parallel_then_handoff_and_owner_isolation(tmp_path, monkeypatch):
    gate = asyncio.Event()
    entered = set()

    async def hold(agent, messages):
        entered.add(agent.name)
        await gate.wait()

    a = FakeAgent("deepseek", reply="需求分支完成", hook=hold)
    b = FakeAgent("zcode", reply="交互分支完成", hook=hold)
    c = FakeAgent("codex", reply="最终合并完成")
    transport = wire(monkeypatch, {"deepseek": a, "zcode": b, "codex": c})
    app = create_app(make_settings(tmp_path, shared_agent_context=True))
    async with app.router.lifespan_context(app):
        bot = app.state.dispatcher
        await bot.handle_message(make_message("/team create 上线计划"))
        match = re.search(r"\bt[0-9a-f]{8}\b", "\n".join(transport.texts()))
        assert match, transport.texts()
        team = match.group()
        await bot.handle_message(make_message(f"/team add {team} a deepseek -- 分析需求"))
        await bot.handle_message(make_message(f"/team add {team} b zcode -- 设计交互"))
        await bot.handle_message(make_message(f"/team add {team} final codex --after a,b -- 汇总"))
        await bot.handle_message(make_message(f"/team run {team}"))
        await asyncio.wait_for(until(lambda: len(entered) == 2), 3)
        assert not c.calls, "依赖未完成时不应调用汇总模型"
        await bot.handle_message(make_message(f"/team run {team}"))
        gate.set()
        await until(lambda: bool(c.calls))
        await bot.handle_message(make_message(f"/team result {team} final"))
        assert any("最终合并完成" in text for text in transport.texts())
        assert len(a.calls) == len(b.calls) == len(c.calls) == 1, "重复run不得重复调用"
        prompt = "\n".join(message.content for message in c.calls[0])
        assert "需求分支完成" in prompt and "交互分支完成" in prompt
        assert "交互分支完成" not in "\n".join(message.content for message in a.calls[0])
        assert "需求分支完成" not in "\n".join(message.content for message in b.calls[0])
        assert bot.store.recent(
            group_id=200, user_id=100, agent="__shared__", max_turns=8
        ) == []
        await bot.handle_message(make_message("/access add 300"))
        transport.sends.clear()
        await bot.handle_message(make_message(f"/team result {team}", user_id=300))
        assert all(
            "最终合并完成" not in text and "需求分支完成" not in text
            for text in transport.texts()
        )


async def test_qq_file_mode_team_uses_broker_and_acl(tmp_path, monkeypatch):
    """生产装配下的文件步骤：经公共 broker 落盘，结果如实报告已变更文件。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    editor = ScriptedAgent("editor", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": "v2"}
        ]}]}),
        json.dumps({"final": "已更新 a.txt"}),
    ])
    transport = wire(monkeypatch, {"editor": editor})
    # 成员（普通用户 300）拥有 read/edit，管理员同权
    settings = workspace_settings(tmp_path, root=root)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        bot = app.state.dispatcher
        await bot.handle_message(make_message("/access add 300"))
        await bot.handle_message(make_message("/team create 修改项目文件", user_id=300))
        team = re.search(r"\bt[0-9a-f]{8}\b", "\n".join(transport.texts())).group()
        await bot.handle_message(make_message(
            f'/team add {team} e editor --mode edit --dir "{root}" -- 把 a.txt 改成 v2',
            user_id=300,
        ))
        await bot.handle_message(make_message(f"/team run {team}", user_id=300))
        await asyncio.wait_for(
            until(lambda: app.state.workflows.get_workflow(team).status == "succeeded"), 3
        )
        assert (root / "a.txt").read_text(encoding="utf-8") == "v2"
        record = app.state.workflows.get_step_run(team, 1, "e")
        assert record.status == "succeeded" and record.changed_files == 1
        plan = json.loads(app.state.workflows.get_run(team, 1).plan_json)["steps"][0]
        assert plan["mode"] == "edit" and plan["grant_label"] == "test-root"
        transport.sends.clear()
        await bot.handle_message(make_message(f"/team result {team}", user_id=300))
        assert any("已变更 1 个文件" in text for text in transport.texts())
        assert any("已更新 a.txt" in text for text in transport.texts())


async def test_qq_file_step_without_acl_is_refused(tmp_path, monkeypatch):
    """没有目录 ACL 的成员不能通过 /team 获得文件能力（不静默降级）。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    editor = ScriptedAgent("editor", [json.dumps({"final": "不应被调用"})])
    transport = wire(monkeypatch, {"editor": editor})
    # 成员 modes 为空：成员在授权根内没有任何权限
    settings = workspace_settings(tmp_path, root=root, console_workspaces={
        "roots": [{
            "path": str(root), "modes": [], "admin_modes": ["read", "edit", "exec"],
            "searchable": True, "label": "test-root",
        }],
    })
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        bot = app.state.dispatcher
        await bot.handle_message(make_message("/access add 300"))
        await bot.handle_message(make_message("/team create 越权尝试", user_id=300))
        team = re.search(r"\bt[0-9a-f]{8}\b", "\n".join(transport.texts())).group()
        await bot.handle_message(make_message(
            f'/team add {team} e editor --mode edit --dir "{root}" -- 改文件', user_id=300
        ))
        assert any("目录不可用" in text for text in transport.texts())
        assert app.state.workflows.count_steps(team) == 0
        transport.sends.clear()
        await bot.handle_message(make_message(f"/team run {team}", user_id=300))
        assert any("还没有步骤" in text for text in transport.texts())
        assert editor.calls == []
        assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"


async def test_qq_immediate_cancel_leaves_no_queued_team(tmp_path, monkeypatch):
    agent = FakeAgent("deepseek", delay=10)
    transport = wire(monkeypatch, {"deepseek": agent})
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        bot = app.state.dispatcher
        await bot.handle_message(make_message("/team create 立即取消测试"))
        team = re.search(r"\bt[0-9a-f]{8}\b", "\n".join(transport.texts())).group()
        await bot.handle_message(make_message(f"/team add {team} a deepseek -- 等待"))
        await bot.handle_message(make_message(f"/team run {team}"))
        await bot.handle_message(make_message(f"/team cancel {team}"))
        transport.sends.clear()
        await bot.handle_message(make_message(f"/team status {team}"))
        text = "\n".join(transport.texts())
        assert "排队" not in text and "运行中" not in text, text
        assert "取消" in text, text
        assert not agent.calls


async def test_whitelist_revocation_cancels_running_model(tmp_path, monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hold(agent, messages):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    agent = FakeAgent("deepseek", hook=hold)
    transport = wire(monkeypatch, {"deepseek": agent})
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        bot = app.state.dispatcher
        await bot.handle_message(make_message("/access add 300"))
        await bot.handle_message(make_message("/team create 撤销授权测试", user_id=300))
        team = re.search(r"\bt[0-9a-f]{8}\b", "\n".join(transport.texts())).group()
        await bot.handle_message(make_message(f"/team add {team} a deepseek -- 等待", user_id=300))
        await bot.handle_message(make_message(f"/team run {team}", user_id=300))
        await asyncio.wait_for(entered.wait(), 3)
        await bot.handle_message(make_message("/access remove 300"))
        await asyncio.wait_for(cancelled.wait(), 0.5)
        assert app.state.workflows.get_workflow(team).status == "cancelled"


async def test_graceful_restart_preserves_progress_for_explicit_retry(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def hold(agent, messages):
        entered.set()
        await asyncio.Event().wait()

    a = FakeAgent("deepseek", reply="已完成的第一步")
    b = FakeAgent("zcode", hook=hold)
    transport = wire(monkeypatch, {"deepseek": a, "zcode": b})
    settings = make_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        bot = app.state.dispatcher
        await bot.handle_message(make_message("/team create 重启恢复测试"))
        team = re.search(r"\bt[0-9a-f]{8}\b", "\n".join(transport.texts())).group()
        await bot.handle_message(make_message(f"/team add {team} a deepseek -- 第一步"))
        await bot.handle_message(make_message(f"/team add {team} b zcode --after a -- 第二步"))
        await bot.handle_message(make_message(f"/team run {team}"))
        await asyncio.wait_for(entered.wait(), 3)
    b._hook = None
    b._reply = "重启恢复完成"
    resumed = create_app(settings)
    async with resumed.router.lifespan_context(resumed):
        assert resumed.state.workflows.get_workflow(team).status == "interrupted", \
            "正常关闭也应保留为可retry的中断任务"
        await asyncio.sleep(0.02)
        assert len(a.calls) == 1 and len(b.calls) == 1, "重启不能自动调用模型"
        await resumed.state.dispatcher.handle_message(make_message(f"/team retry {team}"))
        await until(lambda: resumed.state.workflows.get_workflow(team).status == "succeeded")
        assert len(a.calls) == 1, "已成功步骤应复用"
        assert len(b.calls) == 2, "只重新执行未完成的步骤"
