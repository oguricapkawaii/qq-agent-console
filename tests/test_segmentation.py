"""长回复分段与推理摘要测试。

新契约：询问 Agent 用 `/task <agent> -- <问题>`；推理预览标题是「📝 推理摘要」，
工具调用摘要是「🔧 操作记录」，归档章节是「## 推理文本 / ## 最终答复 / ## 工具调用」。
"""

from __future__ import annotations

import pytest

from app.security.text_safety import sanitize_outbound_text, split_message
from helpers import FakeAgent, build_dispatcher, make_message, make_settings


def test_split_message_short_text_single_part():
    assert split_message("hello", 100) == ["hello"]


def test_split_message_empty():
    assert split_message("", 100) == [""]


def test_split_message_hard_splits_long_line():
    parts = split_message("a" * 250, 100)
    assert [len(p) for p in parts] == [100, 100, 50]


def test_split_message_prefers_line_boundaries():
    text = "第一行\n第二行\n第三行"
    parts = split_message(text, len("第一行\n第二行"))
    assert parts == ["第一行\n第二行", "第三行"]


def test_split_message_invalid_max_len():
    with pytest.raises(ValueError):
        split_message("abc", 0)


def test_split_message_preserves_content():
    text = "\n".join(f"第{i}行" for i in range(50))
    parts = split_message(text, 30)
    assert "\n".join(parts) == text


async def test_long_reply_is_segmented(tmp_path):
    """超过长度限制的回复被拆成多条消息，带 (i/n) 标记且内容无损。"""
    settings = make_settings(tmp_path, max_message_length=100)
    long_content = "\n".join(f"第{i}行" + "x" * 20 for i in range(30))
    glm = FakeAgent("glm", reply=long_content)
    bot, transport = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 长文"))
    texts = transport.texts()
    assert len(texts) > 1

    total = len(texts)
    assert "(1/" in texts[0]
    assert f"({total}/{total})" in texts[-1]

    # 每段正文（去掉头部行）不超过限制，且每段都带 Agent 展示名与 task_id
    for text in texts:
        body = text.split("\n", 1)[1]
        assert len(body) <= 100
        assert "[GLM · task " in text

    # 内容无损：重组正文等于原文
    bodies = [t.split("\n", 1)[1] for t in texts]
    assert "\n".join(bodies) == long_content


async def test_short_reply_is_single_message(tmp_path):
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", reply="简短回答")
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 短问题"))
    texts = transport.texts()
    assert len(texts) == 1
    assert "(1/1)" not in texts[0]
    assert "简短回答" in texts[0]


async def test_thinking_preview_sent_before_answer(tmp_path):
    """有推理文本时先发一条预览消息，再发答案；预览不进入后续上下文。"""
    settings = make_settings(tmp_path, max_thinking_chars=50)
    glm = FakeAgent("glm", reply="答案", thinking="思" * 80)
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    texts = transport.texts()
    assert len(texts) == 2
    assert "📝 推理摘要" in texts[0]
    assert "已截断" in texts[0]
    assert "思" in texts[0]
    assert texts[1].endswith("答案")
    # 推理文本不写入上下文：下一次调用模型收不到它
    await bot.handle_message(make_message("/task glm -- 追问"))
    assert "思" * 10 not in str(glm.calls[-1])


async def test_thinking_preview_within_budget_not_truncated(tmp_path):
    settings = make_settings(tmp_path, max_thinking_chars=100)
    glm = FakeAgent("glm", reply="答案", thinking="短思考")
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    texts = transport.texts()
    assert len(texts) == 2
    assert texts[0].split("\n", 1)[1] == "短思考"
    assert "已截断" not in texts[0]


async def test_thinking_preview_disabled_or_empty(tmp_path):
    settings = make_settings(tmp_path, max_thinking_chars=0)
    glm = FakeAgent("glm", reply="答案", thinking="思考")
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    assert len(transport.texts()) == 1

    settings2 = make_settings(tmp_path)
    glm2 = FakeAgent("glm2", reply="答案")  # 无推理文本的 Agent（如 API 适配器）
    bot2, transport2 = build_dispatcher(settings2, {"glm2": glm2})
    await bot2.handle_message(make_message("/task glm2 -- 问题"))
    assert len(transport2.texts()) == 1


async def test_thinking_archived_full_text_by_task_id(tmp_path):
    """完整推理文本按 task_id 落盘归档，不受预览截断影响。"""
    archive = tmp_path / "archive"
    settings = make_settings(tmp_path, max_thinking_chars=10, thinking_archive_dir=str(archive))
    glm = FakeAgent("glm", reply="答案", thinking="完" * 100)
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    files = list(archive.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "完" * 100 in body          # 归档是全文
    assert "已截断" not in body         # 截断只发生在群消息预览
    assert "## 推理文本" in body
    assert "- 任务：问题" in body       # 任务正文入档（命令名与选项已剥离）


async def test_thinking_archive_failure_does_not_break_reply(tmp_path):
    """归档目录不可写时只记日志，回复照常发出。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("不是目录")  # mkdir 会失败
    settings = make_settings(tmp_path, thinking_archive_dir=str(blocked / "x"))
    glm = FakeAgent("glm", reply="答案", thinking="思考")
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    assert any("答案" in text for text in transport.texts())


async def test_tool_log_summary_appended_to_reply(tmp_path):
    """文件任务的工具操作摘要附加在答案后面发到群里。"""
    settings = make_settings(tmp_path, max_thinking_chars=0)
    glm = FakeAgent("glm", reply="答案", tool_log=["Edit a.txt [completed]"])
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    text = transport.texts()[0]
    assert "🔧 操作记录" in text
    assert "Edit a.txt [completed]" in text


async def test_tool_log_archived_even_without_thinking(tmp_path):
    """没有推理文本但有工具调用时同样归档，含工具调用章节。"""
    archive = tmp_path / "archive"
    settings = make_settings(tmp_path, thinking_archive_dir=str(archive), max_thinking_chars=0)
    glm = FakeAgent("glm", reply="答案", tool_log=["Write b.txt [completed]"])
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    files = list(archive.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "## 工具调用" in body
    assert "Write b.txt [completed]" in body
    assert "## 推理文本" not in body


async def test_outbound_cq_codes_neutralized(tmp_path):
    """模型输出中的 CQ 码必须被中和，防止 OneBot 解析成富媒体。"""
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", reply="看这个 [CQ:record,file=evil.mp3,url=http://evil]")
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 播放"))
    sent = transport.texts()[0]
    assert "[CQ:record" not in sent
    assert "CQ" in sent  # 内容保留（零宽空格打断解析）


async def test_very_long_reply_is_capped_by_output_limit(tmp_path):
    """超过输出上限的回复明确失败，而不是悄悄截断。"""
    settings = make_settings(tmp_path, team_max_output_chars=50)
    glm = FakeAgent("glm", reply="x" * 200)
    bot, transport = build_dispatcher(settings, {"glm": glm})
    await bot.handle_message(make_message("/task glm -- 问题"))
    joined = "\n".join(transport.texts())
    assert "失败" in joined and "超过上限" in joined


def test_sanitize_outbound_control_chars_removed():
    assert "\x00" not in sanitize_outbound_text("a\x00b")
    assert "\n" in sanitize_outbound_text("a\nb")
