"""/task all 广播测试：并发调用、失败隔离、文本-only 约束与限流。

新契约（见 app/commands/registry.py）：/all → `/task all -- <问题>`，
只做纯文本并发，不接受任何公共选项。
"""

from __future__ import annotations

import asyncio

from helpers import FakeAgent, build_dispatcher, make_message, make_settings


async def test_all_invokes_every_configured_agent(tmp_path):
    settings = make_settings(tmp_path)
    agents = {
        "glm": FakeAgent("glm"),
        "deepseek": FakeAgent("deepseek"),
        "gpt": FakeAgent("gpt"),
        "codex": FakeAgent("codex", configured=False),  # 未配置的不参与
    }
    bot, transport = build_dispatcher(settings, agents)

    await bot.handle_message(make_message("/task all -- 这个方案怎么样"))

    assert len(agents["glm"].calls) == 1
    assert len(agents["deepseek"].calls) == 1
    assert len(agents["gpt"].calls) == 1
    assert agents["codex"].calls == []
    # 广播是纯文本：每个 Agent 都不带目录，也不开工具
    for name in ("glm", "deepseek", "gpt"):
        options = agents[name].options_log[-1]
        assert options.mode == "text"
        assert options.directory is None
        assert options.workspace_tools is False
    joined = "\n".join(transport.texts())
    for display in ("GLM", "DEEPSEEK", "GPT"):
        assert f"[{display} · task " in joined


async def test_all_runs_agents_concurrently(tmp_path):
    """用 Barrier 证明 /task all 是并发调用：串行执行会超时失败。"""
    settings = make_settings(tmp_path, request_timeout_seconds=3.0)
    barrier = asyncio.Barrier(2)

    async def hook(agent, messages):
        # 两个 Agent 都到达屏障才能通过；如果串行调用，这里会超时
        await asyncio.wait_for(barrier.wait(), timeout=2.0)

    glm = FakeAgent("glm", hook=hook)
    gpt = FakeAgent("gpt", hook=hook)
    bot, transport = build_dispatcher(settings, {"glm": glm, "gpt": gpt})

    await bot.handle_message(make_message("/task all -- 并发测试"))

    successes = [t for t in transport.texts() if "fake-reply" in t]
    assert len(successes) == 2
    assert not any("失败" in t for t in transport.texts())


async def test_all_timeout_does_not_affect_other_agents(tmp_path):
    """一个 Agent 超时，其他 Agent 仍正常返回。"""
    settings = make_settings(tmp_path, request_timeout_seconds=0.2)
    slow = FakeAgent("gpt", delay=5.0)
    fast = FakeAgent("glm", reply="快速回答")
    bot, transport = build_dispatcher(settings, {"glm": fast, "gpt": slow})

    await bot.handle_message(make_message("/task all -- 谁先答完"))

    texts = transport.texts()
    assert any("快速回答" in t for t in texts)
    assert any("调用超时" in t for t in texts)
    assert len(fast.calls) == 1


async def test_all_agent_error_is_isolated(tmp_path):
    """一个 Agent 报错（如 API 500），其他 Agent 不受影响。"""
    settings = make_settings(tmp_path)
    broken = FakeAgent("gpt", error="api_error_500")
    fine = FakeAgent("glm", reply="正常回答")
    bot, transport = build_dispatcher(settings, {"glm": fine, "gpt": broken})

    await bot.handle_message(make_message("/task all -- 测试"))

    texts = transport.texts()
    assert any("HTTP 500" in t for t in texts)
    assert any("正常回答" in t for t in texts)


async def test_all_without_configured_agents(tmp_path):
    settings = make_settings(tmp_path)
    bot, transport = build_dispatcher(
        settings, {"codex": FakeAgent("codex", configured=False)}
    )
    await bot.handle_message(make_message("/task all -- 问题"))
    assert any("没有已配置的 Agent" in t for t in transport.texts())


async def test_all_requires_body(tmp_path):
    settings = make_settings(tmp_path)
    bot, transport = build_dispatcher(settings, {"glm": FakeAgent("glm")})
    await bot.handle_message(make_message("/task all"))
    assert any("正文为空" in t for t in transport.texts())


async def test_all_rejects_every_option(tmp_path):
    """/task all 只做纯文本：--dir/--mode/--model/--effort 一律拒绝，且不调用模型。"""
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm"), "gpt": FakeAgent("gpt")}
    bot, transport = build_dispatcher(settings, agents)

    for option in ("--effort high", "--model fake-glm", "--mode read", '--dir "/tmp"'):
        await bot.handle_message(make_message(f"/task all {option} -- 问题"))

    assert agents["glm"].calls == [] and agents["gpt"].calls == []
    assert all("不接受" in t for t in transport.texts())
    assert len(transport.texts()) == 4


async def test_rate_limit_counts_each_model_call(tmp_path):
    """限流按模型调用计：一次 /task all 消耗与 Agent 数相同的配额，
    配额耗尽后不会"部分执行"，而是整体被限流。"""
    settings = make_settings(tmp_path, rate_limit_per_minute=2)
    glm = FakeAgent("glm")
    gpt = FakeAgent("gpt")
    bot, transport = build_dispatcher(settings, {"glm": glm, "gpt": gpt})

    await bot.handle_message(make_message("/task all -- 第一问"))
    assert len(glm.calls) == 1 and len(gpt.calls) == 1

    # 2 次配额已被两个 Agent 用满：第二次广播每个 Agent 都被限流，且不再调用模型
    await bot.handle_message(make_message("/task all -- 第二问"))
    assert len(glm.calls) == 1 and len(gpt.calls) == 1
    assert sum("请求过于频繁" in t for t in transport.texts()) == 2
