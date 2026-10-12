"""对抗性复检：脱敏、调度异常收敛、旧回调、失败归档、超时配置与工作区漂移。

这些用例覆盖"看起来成功但其实不安全"的路径：密钥泄露、状态卡死、覆盖别人改过的
文件、把已产生副作用的失败步骤偷偷重跑。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.workflows.executor import StepExecutor
from app.workflows.models import TeamStatus
from helpers import (
    FakeAgent,
    ScriptedAgent,
    TeamHarness,
    make_team_settings,
    workspace_settings,
)


async def test_answer_redacted_before_storage_and_dependency(tmp_path):
    secret = "registered-secret-abcdef12345"
    h = TeamHarness(make_team_settings(tmp_path), {
        "glm": FakeAgent("glm", reply="result " + secret),
        "deepseek": FakeAgent("deepseek", reply="done"),
    })
    h.redactor.register(secret)
    try:
        t = h.create_team()
        h.add_step(t, "a", "glm")
        h.add_step(t, "b", "deepseek", after=("a",))
        await h.command(f"/team run {t}")
        assert await h.wait_team(t) == TeamStatus.SUCCEEDED
        assert secret not in h.workflow_store.get_step_run(t, 1, "a").answer
        assert secret not in h.agents["deepseek"].last_prompt()
    finally:
        await h.aclose()


async def test_scheduler_error_becomes_failed_not_running(tmp_path, monkeypatch):
    h = TeamHarness(make_team_settings(tmp_path), {"glm": FakeAgent("glm", reply="done")})

    async def broken(*args):
        raise RuntimeError("scheduler failed")

    monkeypatch.setattr(h.runner, "_execute_round", broken)
    try:
        t = h.create_team()
        h.add_step(t, "a", "glm")
        await h.command(f"/team run {t}")
        assert await h.wait_team(t, timeout=0.2) == TeamStatus.FAILED
        assert h.workflow_store.get_step_run(t, 1, "a").error == "internal"
    finally:
        await h.aclose()


async def test_old_callback_keeps_new_round_reference(tmp_path):
    h = TeamHarness(make_team_settings(tmp_path), {"glm": FakeAgent("glm", reply="done")})
    old = asyncio.create_task(asyncio.sleep(0))
    newer = asyncio.create_task(asyncio.sleep(0))
    await old
    await newer
    try:
        h.runner._tasks["t01234567"] = newer
        h.runner._on_done("t01234567", old)
        assert h.runner._tasks["t01234567"] is newer
        h.runner._tasks.clear()
    finally:
        await h.aclose()


async def test_failure_archive_records_only_public_content(tmp_path):
    """失败也必须归档真实状态；没有公开内容时不写空档案，也不编造推理摘要。"""
    from test_team_executor_fake import context, frozen

    archive = tmp_path / "archive"
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    h = TeamHarness(workspace_settings(tmp_path, root=root), {
        "codex": FakeAgent("codex", error="codex_failed"),
        "edit": ScriptedAgent("edit", [
            json.dumps({"actions": [{"action": "propose_changes", "changes": [
                {"path": "a.txt", "content": "坏"}
            ]}]}),
            json.dumps({"final": "本不该写"}),
        ]),
    })
    try:
        executor = StepExecutor(
            tasks=h.tasks, store=h.workflow_store, authz=h.authz,
            settings=h.settings, redactor=h.redactor, archive_dir=str(archive),
        )
        # 纯错误、无任何公开内容：不产生空档案
        result = await executor.execute(context(frozen(agent="codex")))
        assert not result.ok and result.error == "codex_failed"
        assert list(archive.glob("team-*.md")) == []

        # 有工具日志的失败：归档真实状态与工具调用，不编造推理/答复
        denied = await executor.execute(context(
            frozen(agent="edit", mode="read", workspace=str(root))
        ))
        assert not denied.ok and denied.error == "broker_denied"
        files = list(archive.glob("team-*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "- 状态：broker_denied" in text
        assert "## 工具调用" in text and "propose_changes" in text
        assert "## 推理文本" not in text and "## 最终答复" not in text
        assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    finally:
        await h.aclose()


@pytest.mark.parametrize("field,value", [
    ("team_step_timeout_seconds", -1),
    ("team_step_timeout_seconds", float("nan")),
    ("team_workflow_timeout_seconds", float("inf")),
])
def test_team_timeouts_reject_invalid_values(tmp_path, field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_team_settings(tmp_path, **{field: value})


# ---------- 文件步骤的 retry 安全规则 ----------


def _edit_script(content: str) -> list[str]:
    return [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": content}
        ]}]}, ensure_ascii=False),
        json.dumps({"final": "改完了"}, ensure_ascii=False),
    ]


async def _failed_team_with_successful_edit(tmp_path, *, agent_script):
    """构造：文件步骤成功 + 下游文本步骤失败（团队 failed）。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    h = TeamHarness(workspace_settings(tmp_path, root=root), {
        "edit": ScriptedAgent("edit", agent_script),
        "boom": FakeAgent("boom", error="network"),
    })
    team_id = h.create_team()
    h.add_step(team_id, "e", "edit", mode="edit", workspace=str(root))
    h.add_step(team_id, "f", "boom", after=("e",))
    await h.command(f"/team run {team_id}")
    assert await h.wait_team(team_id) == TeamStatus.FAILED
    return h, root, team_id


async def test_retry_refuses_reuse_when_successful_file_step_drifted(tmp_path):
    h, root, team_id = await _failed_team_with_successful_edit(
        tmp_path, agent_script=_edit_script("v2")
    )
    try:
        assert (root / "a.txt").read_text(encoding="utf-8") == "v2"
        # 外部改动成功步骤写过的文件：拒绝复用（也不偷偷覆盖）
        (root / "a.txt").write_text("别人改的\n", encoding="utf-8")
        text = await h.command(f"/team retry {team_id}")
        assert "工作区漂移" in text and "a.txt" in text
        assert h.workflow_store.get_workflow(team_id).status == TeamStatus.FAILED
        assert h.workflow_store.get_workflow(team_id).round == 1
        assert (root / "a.txt").read_text(encoding="utf-8") == "别人改的\n"
        # 人工恢复后可以继续，成功步骤被复用
        (root / "a.txt").write_text("v2", encoding="utf-8")
        assert "第 2 轮" in await h.command(f"/team retry {team_id}")
        assert await h.wait_team(team_id) == TeamStatus.FAILED
        round2 = h.workflow_store.list_step_runs(team_id, 2)
        assert round2["e"].source_round == 1
        assert len(h.agents["edit"].calls) == 2  # 复用，没有第二次执行
    finally:
        await h.aclose()


async def test_retry_refuses_reuse_when_successful_file_deleted(tmp_path):
    h, root, team_id = await _failed_team_with_successful_edit(
        tmp_path, agent_script=_edit_script("v2")
    )
    try:
        (root / "a.txt").unlink()
        text = await h.command(f"/team retry {team_id}")
        assert "工作区漂移" in text and "已被删除或被外部替换" in text
        assert h.workflow_store.get_workflow(team_id).round == 1
    finally:
        await h.aclose()


async def test_retry_requires_confirmation_for_failed_exec_side_effects(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    h = TeamHarness(workspace_settings(tmp_path, root=root), {
        "exec": ScriptedAgent("exec", [
            json.dumps({"actions": [{"action": "run_command", "argv": ["ls"], "cwd": "."}]}),
            "这不是 JSON",
        ]),
        "boom": FakeAgent("boom", error="network"),
    })
    try:
        team_id = h.create_team()
        h.add_step(team_id, "x", "exec", mode="exec", workspace=str(root))
        h.add_step(team_id, "f", "boom", after=("x",))
        await h.command(f"/team run {team_id}")
        assert await h.wait_team(team_id) == TeamStatus.FAILED
        assert "第 2 轮" not in await h.command(f"/team retry {team_id}")

        text = await h.command(f"/team retry {team_id}")
        assert "副作用" in text and "run_command" in text
        match = re.search(r"/confirm ([0-9A-F]{8})", text)
        assert match, text
        assert h.workflow_store.get_workflow(team_id).round == 1  # 未确认前不得启动

        confirmed = await h.command(f"/confirm {match.group(1)}")
        assert "第 2 轮" in confirmed
        assert await h.wait_team(team_id) == TeamStatus.FAILED  # boom 仍然失败
        assert h.workflow_store.get_workflow(team_id).round == 2
        # 确认码一次性
        assert "无效" in await h.command(f"/confirm {match.group(1)}")
    finally:
        await h.aclose()


async def test_retry_requires_confirmation_for_partial_write(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    h = TeamHarness(workspace_settings(tmp_path, root=root), {
        "edit": ScriptedAgent("edit", [
            json.dumps({"actions": [{"action": "propose_changes", "changes": [
                {"path": "a.txt", "content": "v2"}
            ]}]}),
            "这不是 JSON",
        ]),
        "boom": FakeAgent("boom", error="network"),
    })
    try:
        team_id = h.create_team()
        h.add_step(team_id, "e", "edit", mode="edit", workspace=str(root))
        h.add_step(team_id, "f", "boom", after=("e",))
        await h.command(f"/team run {team_id}")
        assert await h.wait_team(team_id) == TeamStatus.FAILED
        assert (root / "a.txt").read_text(encoding="utf-8") == "v2"  # 已部分写入
        record = h.workflow_store.get_step_run(team_id, 1, "e")
        assert record.status == "failed" and record.changed_files == 1
        text = await h.command(f"/team retry {team_id}")
        assert "已部分写入" in text and "a.txt" in text
        assert re.search(r"/confirm ([0-9A-F]{8})", text)
        assert h.workflow_store.get_workflow(team_id).round == 1
    finally:
        await h.aclose()


async def test_retry_confirm_code_is_bound_to_team_payload(tmp_path):
    """重跑确认必须携带 team_id，且不能被其它任务的确认码替代。"""
    root = tmp_path / "proj"
    root.mkdir()
    h = TeamHarness(workspace_settings(tmp_path, root=root), {
        "exec": ScriptedAgent("exec", [
            json.dumps({"actions": [{"action": "run_command", "argv": ["ls"], "cwd": "."}]}),
            "这不是 JSON",
        ]),
        "boom": FakeAgent("boom", error="network"),
    })
    try:
        team_id = h.create_team()
        h.add_step(team_id, "x", "exec", mode="exec", workspace=str(root))
        h.add_step(team_id, "f", "boom", after=("x",))
        await h.command(f"/team run {team_id}")
        await h.wait_team(team_id)
        text = await h.command(f"/team retry {team_id}")
        code = re.search(r"/confirm ([0-9A-F]{8})", text).group(1)
        # 别的用户不能消费该确认码（团队重跑只属于发起人）
        h.context_store.set_whitelist(200, 777, True, 100)
        other = await h.command(f"/confirm {code}", user_id=777)
        assert "不属于你" in other, other
        assert h.workflow_store.get_workflow(team_id).round == 1
        record = h.confirmations.peek(code)
        assert record is not None and record.payload.get("team_id") == team_id
    finally:
        await h.aclose()
