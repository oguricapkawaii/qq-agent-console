"""上下文管理测试：Agent 间 / 用户间 / 群间隔离、/agent clear、轮数上限。

新契约：`/clear <agent>` → `/agent clear <agent>`；询问 Agent 用 `/task <agent> -- <问题>`。
"""

from __future__ import annotations

from helpers import ALLOWED_USER, FakeAgent, build_dispatcher, make_message, make_settings


async def test_context_isolated_between_agents(tmp_path):
    """一个 Agent 的历史不得泄漏给另一个 Agent。"""
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", reply="GLM的回答")
    deepseek = FakeAgent("deepseek", reply="DS的回答")
    bot, _ = build_dispatcher(settings, {"glm": glm, "deepseek": deepseek})

    await bot.handle_message(make_message("/task glm -- 问题A"))
    await bot.handle_message(make_message("/task deepseek -- 问题B"))

    # deepseek 的调用中不应出现 glm 的任何内容
    deepseek_messages = deepseek.calls[0]
    assert not any("问题A" in m.content for m in deepseek_messages)
    assert not any("GLM的回答" in m.content for m in deepseek_messages)
    assert any(m.content == "问题B" for m in deepseek_messages)

    # glm 第二次调用能看到自己的历史
    await bot.handle_message(make_message("/task glm -- 问题A2"))
    glm_second = glm.calls[1]
    assert any(m.content == "问题A" for m in glm_second)
    assert any(m.content == "GLM的回答" for m in glm_second)
    assert any(m.content == "问题A2" for m in glm_second)
    # 系统提示词始终存在且固定在首位
    assert glm_second[0].role == "system"


async def test_context_isolated_between_users(tmp_path):
    settings = make_settings(tmp_path, qq_allowed_user_ids=[ALLOWED_USER, 200])
    glm = FakeAgent("glm", reply="ok")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 用户一的秘密", user_id=ALLOWED_USER))
    await bot.handle_message(make_message("/task glm -- 你好", user_id=200))

    # 用户 200 的调用中不能看到用户 1 的内容
    second_call = glm.calls[1]
    assert not any("用户一的秘密" in m.content for m in second_call)


async def test_context_isolated_between_groups(tmp_path):
    settings = make_settings(tmp_path, qq_allowed_group_ids=[200, 300])
    glm = FakeAgent("glm", reply="ok")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 群一的话题", group_id=200))
    await bot.handle_message(make_message("/task glm -- 你好", group_id=300))
    assert not any("群一的话题" in m.content for m in glm.calls[1])


async def test_clear_removes_only_target_agent_context(tmp_path):
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", reply="glm-ok")
    deepseek = FakeAgent("deepseek", reply="ds-ok")
    bot, transport = build_dispatcher(settings, {"glm": glm, "deepseek": deepseek})

    await bot.handle_message(make_message("/task glm -- 问题1"))
    await bot.handle_message(make_message("/task deepseek -- 问题1"))

    await bot.handle_message(make_message("/agent clear glm"))
    assert any("已清除 glm" in t for t in transport.texts())

    # glm 的上下文被清空
    await bot.handle_message(make_message("/task glm -- 问题2"))
    glm_second = glm.calls[1]
    assert not any(m.content == "问题1" for m in glm_second)

    # deepseek 的上下文不受影响
    await bot.handle_message(make_message("/task deepseek -- 问题2"))
    deepseek_second = deepseek.calls[1]
    assert any(m.content == "问题1" for m in deepseek_second)


async def test_clear_is_scoped_to_group_and_user(tmp_path):
    """清除上下文只影响「本群 × 你 × 该 Agent」，别人和别人群的历史保留。"""
    settings = make_settings(
        tmp_path, qq_allowed_user_ids=[ALLOWED_USER, 200], qq_allowed_group_ids=[200, 300]
    )
    glm = FakeAgent("glm", reply="ok")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 我的秘密", group_id=200))
    await bot.handle_message(make_message("/task glm -- 别人的秘密", user_id=200, group_id=200))

    # 用户 200 清除自己的上下文：本人看不到自己的历史，也不影响别人的
    await bot.handle_message(make_message("/agent clear glm", user_id=200, group_id=200))
    await bot.handle_message(make_message("/task glm -- 我的新问题", user_id=200, group_id=200))
    assert not any("别人的秘密" in m.content for m in glm.calls[2])

    # 用户 100 的历史完全没被影响
    await bot.handle_message(make_message("/task glm -- 我的追问", group_id=200))
    assert any("我的秘密" in m.content for m in glm.calls[3])


async def test_clear_unknown_agent_shows_usage(tmp_path):
    settings = make_settings(tmp_path)
    bot, transport = build_dispatcher(settings, {"glm": FakeAgent("glm")})
    await bot.handle_message(make_message("/agent clear 不存在的"))
    assert any("用法" in t for t in transport.texts())


async def test_context_turn_limit(tmp_path):
    """max_context_turns=2：第 4 次调用只能看到最近 2 轮历史。"""
    settings = make_settings(tmp_path, max_context_turns=2)
    glm = FakeAgent("glm", reply="reply")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    for i in range(4):
        await bot.handle_message(make_message(f"/task glm -- 问题{i}"))

    last_call = glm.calls[3]
    user_messages = [m.content for m in last_call if m.role == "user"]
    # 裁剪后只保留最近 2 轮（问题1/回答1、问题2/回答2）+ 当前问题3
    assert user_messages == ["问题1", "问题2", "问题3"]
    assistant_messages = [m.content for m in last_call if m.role == "assistant"]
    assert assistant_messages == ["reply", "reply"]


async def test_failed_call_leaves_no_dangling_history(tmp_path):
    """失败的调用不应留下悬空的用户消息。"""
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", error="network")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 会失败的问题"))
    # 修正 FakeAgent 让下一次成功
    glm._error = None
    glm._reply = "good"
    await bot.handle_message(make_message("/task glm -- 正常问题"))

    second_call = glm.calls[1]
    assert [m.content for m in second_call if m.role == "user"] == ["正常问题"]
