"""共享线程测试：shared_agent_context=true 时各 Agent 互相可见（Agent 间对话）。

默认档（false）下仍是完全隔离，隔离用例见 tests/test_context.py；这里覆盖开启后的行为，
并确保「开关关闭 = 原样（并发、隔离）」这一点没被破坏。
新契约：`/all` → `/task all -- ...`，`/clear <agent>` → `/agent clear <agent>`。
"""

from __future__ import annotations

import asyncio

from app.storage.context_store import SHARED_THREAD, thread_key
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    FakeAgent,
    build_dispatcher,
    make_message,
    make_settings,
)


def _shared_settings(tmp_path, **overrides):
    return make_settings(tmp_path, shared_agent_context=True, **overrides)


async def test_shared_thread_agents_see_each_other(tmp_path):
    """A 问 DeepSeek，再问 Codex：Codex 必须能看到 DeepSeek 的回答，并知道那是谁说的。"""
    settings = _shared_settings(tmp_path)
    deepseek = FakeAgent("deepseek", display_name="DeepSeek", reply="DeepSeek的方案：先做A再做B")
    codex = FakeAgent("codex", display_name="Codex", reply="Codex的评审：同意，但B要加校验")
    bot, _ = build_dispatcher(settings, {"deepseek": deepseek, "codex": codex})

    await bot.handle_message(make_message("/task deepseek -- 帮我出个方案"))
    await bot.handle_message(make_message("/task codex -- 你评审一下上面的方案"))

    codex_view = codex.calls[0]
    assert any("DeepSeek的方案" in m.content for m in codex_view), "Codex 看不到 DeepSeek 的回答"
    assert any("[Agent][DeepSeek]" in m.content for m in codex_view), "缺少发言人标注，分不清谁说的"
    # 提问原文带提问者标注，风格与历史一致
    assert any(m.role == "user" and f"[群成员][{ALLOWED_USER}]" in m.content for m in codex_view)


async def test_shared_thread_reverse_direction(tmp_path):
    """反向也要成立：先问 Codex，再问 DeepSeek，DeepSeek 能看到 Codex 的话。"""
    settings = _shared_settings(tmp_path)
    codex = FakeAgent("codex", display_name="Codex", reply="Codex先说：这个接口要加限流")
    deepseek = FakeAgent("deepseek", display_name="DeepSeek", reply="DeepSeek补充：限流阈值按用户维度")
    bot, _ = build_dispatcher(settings, {"deepseek": deepseek, "codex": codex})

    await bot.handle_message(make_message("/task codex -- 看下这个接口"))
    await bot.handle_message(make_message("/task deepseek -- 补充一下"))

    deepseek_view = deepseek.calls[0]
    assert any("这个接口要加限流" in m.content for m in deepseek_view)
    assert any("[Agent][Codex]" in m.content for m in deepseek_view)


async def test_all_runs_sequentially_in_shared_mode(tmp_path):
    """共享模式下 /task all 必须串行：后一个 Agent 要看到前一个刚说的话。"""
    settings = _shared_settings(tmp_path, rate_limit_per_minute=10)
    first = FakeAgent("deepseek", display_name="DeepSeek", reply="第一个说：方案是A")
    second = FakeAgent("zcode", display_name="ZCode", reply="第二个说：我改一下")
    bot, transport = build_dispatcher(settings, {"deepseek": first, "zcode": second})

    await bot.handle_message(make_message("/task all -- 一起看这个问题"))

    assert len(first.calls) == 1 and len(second.calls) == 1
    second_view = second.calls[0]
    assert any("方案是A" in m.content for m in second_view), "/task all 串行未生效，后一个 Agent 看不到前一个"
    assert "[DeepSeek · task " in transport.texts()[0]
    assert "[ZCode · task " in transport.texts()[1]


async def test_all_still_concurrent_by_default(tmp_path):
    """开关关闭时 /task all 仍并发（原行为不变）：串行执行会因为互相等待而失败。"""
    settings = make_settings(tmp_path, rate_limit_per_minute=10)
    both_running = asyncio.Event()
    started: list[str] = []

    async def rendezvous(agent, messages):
        started.append(agent.name)
        if len(started) == 2:
            both_running.set()
        # 只有两个 Agent 同时进入，才能在短时间内互相等到对方
        await asyncio.wait_for(both_running.wait(), timeout=0.5)

    first = FakeAgent("deepseek", hook=rendezvous, reply="a")
    second = FakeAgent("zcode", hook=rendezvous, reply="b")
    bot, transport = build_dispatcher(settings, {"deepseek": first, "zcode": second})

    await bot.handle_message(make_message("/task all -- 并发问题"))

    assert sorted(started) == ["deepseek", "zcode"]
    assert not any("失败" in t for t in transport.texts()), "串行执行会让两个 Agent 互相等待超时"


async def test_default_mode_keeps_agents_isolated(tmp_path):
    """开关关闭 = 原样：一个 Agent 的历史不得出现在另一个 Agent 的上下文里。"""
    settings = make_settings(tmp_path)
    deepseek = FakeAgent("deepseek", reply="DeepSeek的回答")
    codex = FakeAgent("codex", reply="Codex的回答")
    bot, _ = build_dispatcher(settings, {"deepseek": deepseek, "codex": codex})

    await bot.handle_message(make_message("/task deepseek -- 问题甲"))
    await bot.handle_message(make_message("/task codex -- 问题乙"))

    codex_view = codex.calls[0]
    assert not any("DeepSeek的回答" in m.content for m in codex_view)
    assert not any("问题甲" in m.content for m in codex_view)


async def test_clear_wipes_whole_shared_thread(tmp_path):
    """共享线程下 /agent clear 任一 Agent 即清整条线程，且文案说明范围。"""
    settings = _shared_settings(tmp_path)
    deepseek = FakeAgent("deepseek", reply="DS答")
    codex = FakeAgent("codex", reply="CX答")
    bot, transport = build_dispatcher(settings, {"deepseek": deepseek, "codex": codex})

    await bot.handle_message(make_message("/task deepseek -- 甲问题"))
    await bot.handle_message(make_message("/task codex -- 乙问题"))
    await bot.handle_message(make_message("/agent clear deepseek"))

    assert any("共享线程" in t for t in transport.texts())

    await bot.handle_message(make_message("/task deepseek -- 丙问题"))
    view = deepseek.calls[1]
    assert not any("甲问题" in m.content for m in view)
    assert not any("CX答" in m.content for m in view)
    assert any("丙问题" in m.content for m in view)


async def test_shared_mode_still_isolated_between_groups_and_users(tmp_path):
    """共享只放宽 Agent 维度：群与用户之间仍然互不可见。"""
    settings = _shared_settings(
        tmp_path, qq_allowed_user_ids=[ALLOWED_USER, 200],
        qq_allowed_group_ids=[ALLOWED_GROUP, 300],
    )
    glm = FakeAgent("glm", reply="ok")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 甲群的秘密", group_id=ALLOWED_GROUP))
    await bot.handle_message(make_message("/task glm -- 乙群的问题", group_id=300))
    assert not any("甲群的秘密" in m.content for m in glm.calls[1])

    await bot.handle_message(make_message("/task glm -- 另一个用户的秘密", user_id=ALLOWED_USER))
    await bot.handle_message(make_message("/task glm -- 我的问题", user_id=200))
    assert not any("另一个用户的秘密" in m.content for m in glm.calls[3])


async def test_status_reports_shared_mode(tmp_path):
    settings = _shared_settings(tmp_path)
    bot, transport = build_dispatcher(settings, {"glm": FakeAgent("glm")})
    await bot.handle_message(make_message("/agent status"))
    assert any("共享线程" in t for t in transport.texts())


def test_thread_key_maps_all_agents_to_one_thread():
    assert thread_key("deepseek", shared=True) == thread_key("codex", shared=True) == SHARED_THREAD
    assert thread_key("deepseek", shared=False) == "deepseek"
