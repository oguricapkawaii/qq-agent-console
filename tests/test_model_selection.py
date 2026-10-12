"""模型/强度选择测试。

新契约：/set → `/agent set <agent> [--model M] [--effort E|default]`，
/models → `/agent models <agent>`，/status → `/agent status [agent]`（HTTP /status 不变）。
覆盖：解析、保存默认、显式覆盖、按「群 × 用户」隔离、重启后仍生效、非法参数不落库。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agents.openai_compat import OpenAICompatibleAdapter
from app.commands.options import (
    OptionError,
    parse_public_options,
    parse_task_arguments,
    split_body,
)
from app.commands.registry import CommandRegistry
from app.models import ChatMessage, InvocationOptions
from app.storage import ContextStore
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    make_settings,
    OTHER_GROUP,
    OTHER_USER,
)

TASK_SPEC = CommandRegistry().get("task")


def selectable_agent(name: str = "gpt") -> FakeAgent:
    """可选模型/强度固定的假 Agent（model_name 默认 fake-<name>）。"""
    return FakeAgent(name, model_options={"fake-gpt": ("low", "high"), "alternate": ("low",)})


# ---------- 统一选项解析 ----------

def test_parser_splits_options_and_preserves_body():
    parsed = parse_task_arguments(
        '--model=fake-gpt --effort high -- 解释 "a b"\n第二行 --effort low', spec=TASK_SPEC
    )
    assert (parsed.model, parsed.effort) == ("fake-gpt", "high")
    # 分隔符之后的整段原样保留（引号、换行、看起来像选项的内容都不再被解释）
    assert parsed.body == '解释 "a b"\n第二行 --effort low'


def test_body_after_separator_is_never_parsed_as_options():
    assert parse_task_arguments("-- --model high", spec=TASK_SPEC).body == "--model high"
    assert split_body("--mode text -- 正文") == ("--mode text ", " 正文")
    # 没有独立 `--` 时由调用方决定报错
    assert split_body("--model x 正文")[1] is None


def test_options_can_be_queried_without_body():
    parsed = parse_public_options("--effort high", spec=CommandRegistry().get("agent"))
    assert parsed.effort == "high" and parsed.effort_set is True
    # 不是每个命令都接受 task 的公共选项
    with pytest.raises(OptionError):
        parse_public_options("--dir /tmp", spec=CommandRegistry().get("agent"))


@pytest.mark.parametrize("text", [
    "--effort",
    "--unknown high -- hi",
    "--model x --model y -- hi",
    "--model --effort high -- hi",
    "--mode bogus -- hi",
    "--dir /a --find b -- hi",
    "--mode text --dir /a -- hi",
])
def test_malformed_options_rejected(text):
    with pytest.raises(OptionError):
        parse_task_arguments(text, spec=TASK_SPEC)


# ---------- 保存默认 / 覆盖 / 隔离 / 重启 ----------

async def test_defaults_override_isolation_and_restart(tmp_path):
    settings = make_settings(
        tmp_path,
        qq_allowed_user_ids=[ALLOWED_USER, OTHER_USER],
        qq_allowed_group_ids=[ALLOWED_GROUP, OTHER_GROUP],
    )
    agent = selectable_agent()
    harness = ConsoleHarness(settings, {"gpt": agent})

    await harness.command("/agent set gpt --effort high")
    await harness.command("/task gpt -- first")
    await harness.command("/task gpt --effort low -- second")
    await harness.command("/task gpt -- third")
    await harness.command("/task gpt -- other user", user_id=OTHER_USER)
    await harness.command("/task gpt -- other group", group_id=OTHER_GROUP)

    # 保存的默认只影响本人本群；显式 --effort 覆盖一次
    assert [item.effort for item in agent.options_log] == ["high", "low", "high", None, None]
    assert agent.calls[0][-1].content == "first"
    assert agent.calls[1][-1].content == "second"

    await harness.command("/task gpt --model alternate -- change")
    assert agent.options_log[-1].model == "alternate"
    assert agent.options_log[-1].effort is None  # 换模型时强度回到模型默认

    # 回复头如实展示实际使用的强度
    assert "强度：high" in "\n".join(harness.transport.texts())

    # 重启（重开数据库）后保存的默认仍在
    harness.context_store.close()
    reopened = ContextStore(settings.database_path)
    assert reopened.preferences(
        group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="gpt"
    ) == InvocationOptions("fake-gpt", "high", backend="gpt")
    reopened.close()


async def test_invalid_and_unauthorized_do_not_call_or_save(tmp_path):
    settings = make_settings(tmp_path)
    agent = selectable_agent()
    harness = ConsoleHarness(settings, {"gpt": agent})

    for text in [
        "/task gpt --model evil -- hi",                      # 模型不在真实目录
        "/agent set gpt --model alternate --effort high",    # 强度不受该模型支持
        "/task gpt --effort ultra -- hi",                    # 未知强度
        "/task all --effort high -- hi",                     # 广播不接受选项
        '/agent set gpt --dir "/tmp"',                       # 目录不能保存为默认
    ]:
        await harness.command(text)

    # 未授权用户既不能调用，也不能写偏好
    await harness.command("/agent set gpt --effort high", user_id=42)

    assert agent.calls == []
    assert agent.options_log == []
    assert harness.context_store.preferences(
        group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="gpt"
    ) is None
    assert "未授权" in harness.transport.texts()[-1]
    assert "未知选项" in harness.transport.texts()[-2]


async def test_all_uses_individual_preferences_and_concurrent_calls(tmp_path):
    settings = make_settings(tmp_path, rate_limit_per_minute=10)
    agent = selectable_agent()
    harness = ConsoleHarness(settings, {"gpt": agent})

    await harness.command("/agent set gpt --effort high")
    await harness.command("/task all -- hi")
    await asyncio.gather(
        harness.command("/task gpt --effort low -- a"),
        harness.command("/task gpt --model alternate -- b"),
    )

    assert sorted((item.model, item.effort) for item in agent.options_log) == [
        ("alternate", None), ("fake-gpt", "high"), ("fake-gpt", "low"),
    ]
    assert agent.model_name == "fake-gpt"


async def test_models_status_and_reset(tmp_path):
    settings = make_settings(tmp_path)
    harness = ConsoleHarness(settings, {"gpt": selectable_agent()})

    await harness.command("/agent models gpt")
    assert "alternate：low" in harness.transport.texts()[-1]

    await harness.command("/agent set gpt --effort high")
    await harness.command("/agent status gpt")
    assert "fake-gpt / high" in harness.transport.texts()[-1]

    # --effort default 表示回到模型默认
    await harness.command("/agent set gpt --effort default")
    saved = harness.context_store.preferences(
        group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="gpt"
    )
    assert saved is not None and saved.effort is None and saved.model == "fake-gpt"


async def test_agent_usage_errors(tmp_path):
    settings = make_settings(tmp_path)
    harness = ConsoleHarness(settings, {"gpt": selectable_agent()})

    await harness.command("/agent models")
    assert "用法" in harness.transport.texts()[-1]
    await harness.command("/agent set 不存在的 --model x")
    assert "用法" in harness.transport.texts()[-1]
    await harness.command("/agent clear 不存在的")
    assert "用法" in harness.transport.texts()[-1]


# ---------- 适配器 payload ----------

async def test_api_payload_contains_selected_options():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    agent = OpenAICompatibleAdapter(
        name="gpt", display_name="GPT", base_url="https://test/v1",
        api_key="test", model="a", model_options={"a": [], "b": ["high"]},
        http_transport=httpx.MockTransport(handler),
    )
    await agent.chat_with_options(
        [ChatMessage("user", "hi")], timeout=1, options=InvocationOptions("b", "high")
    )
    await agent.chat([ChatMessage("user", "hi")], timeout=1)
    assert payloads[0]["model"] == "b" and payloads[0]["reasoning_effort"] == "high"
    assert payloads[1]["model"] == "a" and "reasoning_effort" not in payloads[1]
    await agent.aclose()
