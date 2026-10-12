"""团队重试语义：成功复用、工作区漂移拒绝、副作用确认、已取消不可重试。

这些用例与 A17（team 文件重试）对应，检查的是真实文件哈希与真实状态转移。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.workflows.models import StepStatus, TeamStatus
from helpers import ScriptedAgent, workspace_settings


def _settings(tmp_path, root):
    return workspace_settings(tmp_path, root=root)


# ---------- 漂移检测 ----------


async def test_successful_file_step_is_reused_when_unchanged(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1", encoding="utf-8")
    settings = _settings(tmp_path, root)

    edit = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": "v2"}]}]}),
        json.dumps({"final": "已改"}),
    ])
    verify = ScriptedAgent("codex", [
        "不是 JSON",  # 第二步失败，触发 retry
    ])
    from helpers import ConsoleHarness

    harness = ConsoleHarness(settings, {"zcode": edit, "codex": verify})
    try:
        team = harness.create_team("漂移检查")
        harness.add_step(team, "edit", "zcode", "改", mode="edit",
                         workspace=str(root.resolve()))
        harness.add_step(team, "verify", "codex", "验证", mode="edit",
                         workspace=str(root.resolve()), after=("edit",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        # 第一步成功且文件未再变化 → retry 时复用
        retry = await harness.command(f"/team retry {team}")
        assert "复用成功步骤：edit" in retry
    finally:
        await harness.aclose()


async def test_workspace_drift_refuses_reuse(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1", encoding="utf-8")
    settings = _settings(tmp_path, root)

    edit = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": "v2"}]}]}),
        json.dumps({"final": "已改"}),
    ])
    verify = ScriptedAgent("codex", ["坏输出"])
    from helpers import ConsoleHarness

    harness = ConsoleHarness(settings, {"zcode": edit, "codex": verify})
    try:
        team = harness.create_team("漂移")
        harness.add_step(team, "edit", "zcode", "改", mode="edit",
                         workspace=str(root.resolve()))
        harness.add_step(team, "verify", "codex", "验证", mode="edit",
                         workspace=str(root.resolve()), after=("edit",))
        await harness.command(f"/team run {team}")
        await harness.wait_team(team)
        # 外部把成功步骤写过的文件改掉
        (root / "a.txt").write_text("被人工改过", encoding="utf-8")
        retry = await harness.command(f"/team retry {team}")
        assert "漂移" in retry
        assert "新建计划" in retry
        # 没有偷偷重跑：仍然是失败状态，且文件内容保持人工修改后的值
        assert harness.workflow_store.get_workflow(team).status == TeamStatus.FAILED
        assert (root / "a.txt").read_text(encoding="utf-8") == "被人工改过"
    finally:
        await harness.aclose()


# ---------- 副作用确认 ----------


async def test_partial_write_requires_confirmation_before_retry(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("v1", encoding="utf-8")
    settings = _settings(tmp_path, root)
    script = [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": "v2"}]}]}),
        "坏掉的输出",
    ]
    from helpers import ConsoleHarness

    harness = ConsoleHarness(settings, {"zcode": ScriptedAgent("zcode", script)})
    try:
        team = harness.create_team("副作用")
        harness.add_step(team, "edit", "zcode", "改", mode="edit",
                         workspace=str(root.resolve()))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        assert (root / "a.txt").read_text(encoding="utf-8") == "v2"

        retry = await harness.command(f"/team retry {team}")
        assert "副作用" in retry or "无法证明" in retry
        code = _last_confirm_code(harness)
        assert code
        # 确认前不会启动新一轮
        assert harness.workflow_store.get_workflow(team).round == 1

        confirmed = await harness.command(f"/confirm {code}")
        assert "已开启第 2 轮" in confirmed
        await harness.wait_team(team)
        assert harness.workflow_store.get_workflow(team).round == 2
    finally:
        await harness.aclose()


async def test_exec_step_always_needs_confirmation_on_retry(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = _settings(tmp_path, root)
    script = [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c", "print(1)"], "timeout": 10}]}),
        "坏输出",
    ]
    from helpers import ConsoleHarness

    harness = ConsoleHarness(settings, {"codex": ScriptedAgent("codex", script)})
    try:
        team = harness.create_team("exec 副作用")
        harness.add_step(team, "run", "codex", "跑", mode="exec",
                         workspace=str(root.resolve()))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        retry = await harness.command(f"/team retry {team}")
        assert "exec" in retry
        assert _last_confirm_code(harness)
    finally:
        await harness.aclose()


async def test_text_failure_retries_without_confirmation(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = _settings(tmp_path, root)
    from helpers import ConsoleHarness, FakeAgent

    agent = FakeAgent("deepseek", reply="文本答复")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("纯文本")
        harness.add_step(team, "think", "deepseek", "想", mode="text")
        agent._error = "network"  # noqa: SLF001 - 模拟一次失败
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        agent._error = None  # noqa: SLF001
        retry = await harness.command(f"/team retry {team}")
        assert "已开启第 2 轮" in retry
        assert "确认" not in retry
        await harness.wait_team(team)
        assert harness.workflow_store.get_workflow(team).status == TeamStatus.SUCCEEDED
    finally:
        await harness.aclose()


async def test_cancelled_team_cannot_retry(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = _settings(tmp_path, root)
    from helpers import ConsoleHarness, FakeAgent

    agent = FakeAgent("deepseek", reply="ok", delay=0.2)
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team = harness.create_team("取消")
        harness.add_step(team, "s", "deepseek", "做")
        await harness.command(f"/team run {team}")
        await asyncio.sleep(0.02)
        await harness.command(f"/team cancel {team}")
        assert await harness.wait_team(team) == TeamStatus.CANCELLED
        refused = await harness.command(f"/team retry {team}")
        assert "已取消" in refused
    finally:
        await harness.aclose()


async def test_successful_step_reuse_records_source_round(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = _settings(tmp_path, root)
    from helpers import ConsoleHarness, FakeAgent

    good = FakeAgent("deepseek", reply="第一步结论")
    bad = FakeAgent("zcode", reply="第二步结论", error="network")
    harness = ConsoleHarness(settings, {"deepseek": good, "zcode": bad})
    try:
        team = harness.create_team("复用")
        harness.add_step(team, "one", "deepseek", "第一步")
        harness.add_step(team, "two", "zcode", "第二步", after=("one",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        bad._error = None  # noqa: SLF001
        await harness.command(f"/team retry {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        first = harness.workflow_store.get_step_run(team, 2, "one")
        assert first.source_round == 1
        assert first.answer == "第一步结论"
    finally:
        await harness.aclose()


def _last_confirm_code(harness) -> str:
    for _group, text in reversed(harness.transport.sends):
        if "/confirm " in text:
            return text.split("/confirm ")[1].split()[0].strip()
    return ""
