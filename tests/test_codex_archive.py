"""任务归档测试：任务按 task_id 归档、密钥脱敏、权限 0600、失败如实记录。

新契约：归档由 TaskService 统一完成（不再有 codex 专属的"未提供推理摘要"占位文案：
章节按实际内容出现，没有任何可归档内容时不产生空文件）；`/set` 的替代是 `/agent set`，
其中不接受目录/模式。
"""

from __future__ import annotations

import json

from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    make_settings,
    workspace_settings,
)


async def test_success_task_is_archived_redacted_and_mode_0600(tmp_path):
    settings = make_settings(tmp_path, thinking_archive_dir=str(tmp_path / "archive"))
    agent = FakeAgent("codex", reply="finished token-secret", thinking="公开推理摘要")
    harness = ConsoleHarness(settings, {"codex": agent})
    harness.redactor.register("token-secret")

    await harness.command("/task codex -- question")

    files = list((tmp_path / "archive").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert "token-secret" not in text          # 归档前统一脱敏
    assert "- 任务：question" in text
    assert "- 状态：成功" in text
    assert "## 最终答复" in text and "finished" in text
    harness.context_store.close()


async def test_failed_task_is_archived_with_status_and_tool_log(tmp_path):
    """失败任务同样入档：状态与已发生的动作如实记录，不伪造成功。"""
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(
        tmp_path, root=root, thinking_archive_dir=str(tmp_path / "archive")
    )
    agent = ScriptedAgent("codex", [
        json.dumps({"actions": [{"action": "read", "path": "../secret.txt"}]}),
    ])
    harness = ConsoleHarness(settings, {"codex": agent})

    out = await harness.command(f'/task codex --dir "{root}" --mode read -- question')

    assert "失败" in out
    files = list((tmp_path / "archive").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "- 状态：broker_denied" in text
    assert "## 工具调用" in text
    assert "不允许包含 .." in text
    harness.context_store.close()


async def test_text_failure_without_output_creates_no_empty_archive(tmp_path):
    """没有任何可归档内容的失败任务不产生空文件（原 codex 占位文案已取消）。"""
    settings = make_settings(tmp_path, thinking_archive_dir=str(tmp_path / "archive"))
    harness = ConsoleHarness(
        settings, {"codex": FakeAgent("codex", error="timeout")}
    )

    out = await harness.command("/task codex -- question")

    assert "超时" in out
    assert list((tmp_path / "archive").glob("*.md")) == []
    harness.context_store.close()


async def test_archive_sections_follow_actual_content(tmp_path):
    """有推理文本就归档全文；没有就整段省略（不写占位文案）。"""
    settings = make_settings(
        tmp_path, thinking_archive_dir=str(tmp_path / "archive"), max_thinking_chars=10
    )
    with_thinking = FakeAgent("deepseek", reply="答案", thinking="公开推理摘要" * 10)
    harness = ConsoleHarness(settings, {"deepseek": with_thinking})
    await harness.command("/task deepseek -- 问题")
    body = next((tmp_path / "archive").glob("*.md")).read_text(encoding="utf-8")
    assert "## 推理文本" in body
    assert "公开推理摘要" * 10 in body          # 归档是全文，不受预览截断影响
    harness.context_store.close()

    settings2 = make_settings(tmp_path, thinking_archive_dir=str(tmp_path / "archive2"))
    without = FakeAgent("deepseek", reply="答案")
    harness2 = ConsoleHarness(settings2, {"deepseek": without})
    await harness2.command("/task deepseek -- 问题")
    body2 = next((tmp_path / "archive2").glob("*.md")).read_text(encoding="utf-8")
    assert "## 推理文本" not in body2
    assert "## 最终答复" in body2
    harness2.context_store.close()


async def test_directory_and_mode_are_not_saved_in_preferences(tmp_path):
    settings = make_settings(tmp_path)
    agent = FakeAgent("codex")
    harness = ConsoleHarness(settings, {"codex": agent})

    for option in (f'--dir "{tmp_path}"', "--mode read"):
        await harness.command(f"/agent set codex {option}")
        assert "未知选项" in harness.transport.texts()[-1]
    assert harness.context_store.preferences(
        group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex"
    ) is None

    # 保存的偏好只有模型/强度/后端：目录永远来自 /task 的显式 --dir 或 /workspace 绑定
    await harness.command("/agent set codex --model fake-codex")
    saved = harness.context_store.preferences(
        group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex"
    )
    assert saved is not None and saved.model == "fake-codex"
    assert saved.directory is None and saved.mode == "text"

    await harness.command("/task codex -- 你好")
    assert agent.options_log[-1].mode == "text"
    assert agent.options_log[-1].directory is None
    harness.context_store.close()
