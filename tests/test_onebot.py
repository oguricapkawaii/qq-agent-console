"""OneBot 事件解析与全链路（事件→命令→回复）测试。"""

from __future__ import annotations

from app.transports.onebot_common import parse_onebot_group_message
from helpers import FakeAgent, build_dispatcher, make_settings


def base_event(**overrides) -> dict:
    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 12345,
        "user_id": 100,
        "group_id": 200,
        "self_id": 999,
        "raw_message": "/task glm -- 你好",
        "message": "/task glm -- 你好",
        "sender": {"nickname": "张三"},
    }
    event.update(overrides)
    return event


def test_group_text_event_parsed():
    msg = parse_onebot_group_message(base_event())
    assert msg is not None
    assert (msg.group_id, msg.user_id, msg.self_id) == (200, 100, 999)
    assert msg.text == "/task glm -- 你好"
    assert msg.nickname == "张三"


def test_array_message_extracts_text_only():
    event = base_event(
        message=[
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " /task glm -- 分析"}},
            {"type": "image", "data": {"file": "x.jpg"}},
        ],
        raw_message="[CQ:at,qq=999] /task glm -- 分析[CQ:image,file=x.jpg]",
    )
    msg = parse_onebot_group_message(event)
    assert msg.text == "/task glm -- 分析"


def test_message_sent_event_ignored():
    """自身消息回显（message_sent）必须忽略。"""
    assert parse_onebot_group_message(base_event(post_type="message_sent")) is None


def test_private_message_ignored():
    assert parse_onebot_group_message(base_event(message_type="private")) is None


def test_notice_event_ignored():
    assert parse_onebot_group_message(base_event(post_type="notice", notice_type="group_upload")) is None


def test_meta_event_ignored():
    assert parse_onebot_group_message(base_event(post_type="meta_event", meta_event_type="heartbeat")) is None


def test_action_response_ignored():
    """WS 动作的响应（echo 回包）没有 post_type，必须忽略。"""
    assert parse_onebot_group_message({"echo": "abc", "status": "ok", "data": {}}) is None


def test_cq_codes_stripped_from_string_message():
    msg = parse_onebot_group_message(base_event(raw_message="[CQ:image,file=a.jpg] /task glm -- 你好"))
    assert msg.text == "/task glm -- 你好"


def test_missing_ids_returns_none():
    assert parse_onebot_group_message(base_event(user_id=0)) is None
    assert parse_onebot_group_message({"post_type": "message", "message_type": "group"}) is None


def test_non_dict_event_returns_none():
    assert parse_onebot_group_message(None) is None  # type: ignore[arg-type]


async def test_full_flow_via_onebot_event(tmp_path):
    """原始 OneBot 事件 → 解析 → 命令 → Agent → 回复 全链路。"""
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", reply="好的")
    bot, transport = build_dispatcher(settings, {"glm": glm})

    await bot.handle_onebot_event(
        base_event(
            message=[{"type": "text", "data": {"text": "/task glm -- 你好"}}],
            raw_message="/task glm -- 你好",
        )
    )
    assert len(glm.calls) == 1
    assert glm.last_prompt() == "你好"
    assert any("好的" in t for t in transport.texts())


async def test_full_flow_legacy_command_is_migration_hint(tmp_path):
    """旧口令通过 OneBot 事件进来时同样只回迁移提示。"""
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm")
    bot, transport = build_dispatcher(settings, {"glm": glm})

    await bot.handle_onebot_event(base_event(raw_message="/glm 你好", message="/glm 你好"))

    assert glm.calls == []
    assert any("旧口令" in t for t in transport.texts())


async def test_full_flow_ignores_message_sent(tmp_path):
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm")
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_onebot_event(base_event(post_type="message_sent"))
    await bot.handle_onebot_event(base_event(user_id=999, self_id=999))
    assert glm.calls == []
    assert transport.texts() == []
