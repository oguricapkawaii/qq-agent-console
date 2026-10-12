"""A10-A12：模糊查找与选择、目录绑定、全入口目录锁。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.workspaces.coordinator import WorkspaceBusy, WorkspaceCoordinator
from app.workspaces.service import WorkspaceLimits
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    OTHER_GROUP,
    OTHER_USER,
    ConsoleHarness,
    FakeAgent,
    make_settings,
    workspace_settings,
)


def _two_similar_dirs(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "记账项目"
    second = tmp_path / "记账项目备份"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_text("first", encoding="utf-8")
    (second / "a.txt").write_text("second", encoding="utf-8")
    return first, second


def _extract_code(text: str) -> str:
    for line in text.splitlines():
        if "/workspace select" in line:
            parts = line.split("/workspace select")[1].split()
            if parts:
                return parts[0]
    raise AssertionError(f"回执中没有确认码：{text}")


# ---------- A10 模糊查找 ----------


async def test_find_only_searches_authorized_roots(tmp_path) -> None:
    allowed = tmp_path / "工作区"
    allowed.mkdir()
    (allowed / "目标项目").mkdir()
    outside = tmp_path / "外部"
    outside.mkdir()
    (outside / "目标项目副本").mkdir()
    settings = workspace_settings(tmp_path, root=allowed)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        reply = await harness.command("/workspace find 目标")
        assert "目标项目" in reply
        assert "副本" not in reply
    finally:
        await harness.aclose()


async def test_single_candidate_still_requires_selection(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "唯一项目").mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "读完了"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command(
            '/task deepseek --find "唯一" --mode read -- 说明目录结构'
        )
        assert "请发送" in reply and "/workspace select" in reply
        assert agent.calls == []  # 选择前不调用模型
        # 没有绑定、也没有任务结果
        assert harness.task_store.get_binding(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER) is None
        records = harness.task_store.list_recent(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER)
        assert records and records[0]["status"] == "awaiting_selection"
    finally:
        await harness.aclose()


async def test_task_find_select_runs_task_exactly_once(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    first, second = _two_similar_dirs(root)
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "已完成"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command('/task deepseek --find "记账" --mode read -- 说明')
        code = _extract_code(reply)
        assert agent.calls == []
        selected = await harness.command(f"/workspace select {code} 1")
        assert "已选择并绑定目录" in selected
        assert "已完成" in selected
        assert len(agent.calls) == 1
        # 重放同一个确认码：拒绝且不再调用模型
        replay = await harness.command(f"/workspace select {code} 1")
        assert "无效或已被使用" in replay
        assert len(agent.calls) == 1
        # 绑定生效
        bound = harness.task_store.get_binding(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER)
        assert bound in {str(first.resolve()), str(second.resolve())}
    finally:
        await harness.aclose()


async def test_find_selection_rejects_other_user_and_group(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    _two_similar_dirs(root)
    settings = workspace_settings(tmp_path, root=root)
    settings.qq_allowed_user_ids = [ALLOWED_USER, OTHER_USER]
    settings.qq_allowed_group_ids = [ALLOWED_GROUP, OTHER_GROUP]
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        reply = await harness.command('/workspace find "记账"')
        code = _extract_code(reply)
        wrong_user = await harness.command(
            f"/workspace select {code} 1", user_id=OTHER_USER
        )
        assert "不属于你" in wrong_user
        wrong_group = await harness.command(
            f"/workspace select {code} 1", group_id=OTHER_GROUP
        )
        assert "不属于你" in wrong_group or "别的群" in wrong_group
    finally:
        await harness.aclose()


async def test_selection_expires(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    _two_similar_dirs(root)
    settings = workspace_settings(
        tmp_path, root=root,
        console_workspaces={
            "roots": [{"path": str(root), "modes": ["read", "edit", "exec"],
                       "admin_modes": ["read", "edit", "exec"]}],
            "find": {"selection_ttl_seconds": -1},
        },
    )
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        reply = await harness.command('/workspace find "记账"')
        code = _extract_code(reply)
        expired = await harness.command(f"/workspace select {code} 1")
        assert "过期" in expired
    finally:
        await harness.aclose()


async def test_selection_rejected_when_candidate_disappears(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    first, _second = _two_similar_dirs(root)
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        reply = await harness.command('/workspace find "记账"')
        code = _extract_code(reply)
        for child in sorted(first.iterdir()):
            child.unlink()
        first.rmdir()
        result = await harness.command(f"/workspace select {code} 1")
        assert "不存在" in result or "不可用" in result
    finally:
        await harness.aclose()


async def test_standalone_find_select_only_binds(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    _two_similar_dirs(root)
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="不应被调用")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command('/workspace find "记账"')
        code = _extract_code(reply)
        await harness.command(f"/workspace select {code} 1")
        assert agent.calls == []
        assert harness.task_store.list_recent(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER) == []
        assert harness.task_store.get_binding(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER)
    finally:
        await harness.aclose()


async def test_find_without_search_roots_is_refused(tmp_path) -> None:
    settings = make_settings(tmp_path)  # 未配置任何授权根
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        reply = await harness.command("/workspace find 任何")
        assert "没有可搜索的授权根" in reply
    finally:
        await harness.aclose()


# ---------- A11 目录绑定 ----------


async def test_binding_is_isolated_per_group_and_user(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    settings.qq_allowed_user_ids = [ALLOWED_USER, OTHER_USER]
    settings.qq_allowed_group_ids = [ALLOWED_GROUP, OTHER_GROUP]
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek", reply="ok")})
    try:
        await harness.command(f'/workspace use "{sub}"')
        assert harness.task_store.get_binding(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER)
        assert harness.task_store.get_binding(group_id=ALLOWED_GROUP, user_id=OTHER_USER) is None
        assert harness.task_store.get_binding(group_id=OTHER_GROUP, user_id=ALLOWED_USER) is None
    finally:
        await harness.aclose()


async def test_dir_dot_requires_binding_and_never_uses_cwd(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "ok"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command("/task deepseek --dir . --mode read -- 说明")
        assert "需要先绑定目录" in reply
        assert agent.calls == []
        await harness.command(f'/workspace use "{root}"')
        bound_reply = await harness.command("/task deepseek --dir . --mode read -- 说明")
        assert "ok" in bound_reply
    finally:
        await harness.aclose()


async def test_binding_does_not_grant_tools_to_text_tasks(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="文本答复")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        await harness.command(f'/workspace use "{root}"')
        await harness.command("/task deepseek -- 只是聊天")
        options = agent.options_log[-1]
        assert options.mode == "text"
        assert options.directory is None
        assert options.workspace_tools is False
    finally:
        await harness.aclose()


async def test_explicit_file_mode_without_selector_uses_binding(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "跳过"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        refused = await harness.command("/task deepseek --mode edit -- 修改")
        assert "需要目录" in refused
        await harness.command(f'/workspace use "{root}"')
        ok = await harness.command("/task deepseek --mode edit -- 修改")
        assert "跳过" in ok or "失败" in ok
    finally:
        await harness.aclose()


async def test_revoked_acl_invalidates_binding(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "ok"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        await harness.command(f'/workspace use "{root}"')
        # 实时撤权：清空授权根
        harness.acl._grants = ()  # noqa: SLF001 - 模拟管理员移除授权
        status = await harness.command("/workspace status")
        assert "授权已失效" in status
        reply = await harness.command("/task deepseek --dir . --mode read -- 说明")
        assert "需要先绑定目录" in reply or "不在授权根内" in reply
    finally:
        await harness.aclose()


# ---------- A12 全入口目录锁 ----------


async def test_coordinator_conflicts_on_same_and_nested_paths(tmp_path) -> None:
    coordinator = WorkspaceCoordinator(acquire_timeout=0.1)
    root = (tmp_path / "a").resolve()
    nested = (tmp_path / "a" / "sub").resolve()
    other = (tmp_path / "b").resolve()
    async with coordinator.lock((root,), label="first"):
        assert coordinator.conflicts((nested,))
        assert coordinator.conflicts((root,))
        assert not coordinator.conflicts((other,))
        with pytest.raises(WorkspaceBusy):
            async with coordinator.lock((nested,), label="second", timeout=0.05):
                pass
        async with coordinator.lock((other,), label="parallel"):
            assert len(coordinator.active_locks()) == 2
    assert coordinator.active_locks() == []


async def test_coordinator_releases_on_exception_and_cancel(tmp_path) -> None:
    coordinator = WorkspaceCoordinator(acquire_timeout=0.2)
    root = (tmp_path / "a").resolve()
    with pytest.raises(RuntimeError):
        async with coordinator.lock((root,), label="boom"):
            raise RuntimeError("boom")
    assert coordinator.active_locks() == []

    async def worker() -> None:
        async with coordinator.lock((root,), label="cancelled"):
            await asyncio.sleep(5)

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    assert coordinator.active_locks()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.active_locks() == []


async def test_same_directory_tasks_are_serialized(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    order: list[str] = []

    async def hook(agent, messages):
        order.append(agent.name)
        await asyncio.sleep(0.1)

    agents = {
        "deepseek": FakeAgent("deepseek", reply=json.dumps({"final": "A"}), hook=hook),
        "zcode": FakeAgent("zcode", reply=json.dumps({"final": "B"}), hook=hook),
    }
    harness = ConsoleHarness(settings, agents)
    try:
        first, second = await asyncio.gather(
            harness.command(f'/task deepseek --dir "{root}" --mode read -- 读'),
            harness.command(f'/task zcode --dir "{root}" --mode read -- 读'),
        )
        assert "A" in first and "B" in second
        # 串行化：两个任务依次进入，且结束时锁全部释放
        assert len(order) == 2
        assert harness.workspaces._coordinator.active_locks() == []  # noqa: SLF001
    finally:
        await harness.aclose()


async def test_unrelated_directories_run_in_parallel(tmp_path) -> None:
    settings = workspace_settings(tmp_path, root=tmp_path)
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    running = {"max": 0, "now": 0}

    async def hook(agent, messages):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.1)
        running["now"] -= 1

    agents = {
        "deepseek": FakeAgent("deepseek", reply=json.dumps({"final": "A"}), hook=hook),
        "zcode": FakeAgent("zcode", reply=json.dumps({"final": "B"}), hook=hook),
    }
    harness = ConsoleHarness(settings, agents)
    try:
        await asyncio.gather(
            harness.command(f'/task deepseek --dir "{one}" --mode read -- 读'),
            harness.command(f'/task zcode --dir "{two}" --mode read -- 读'),
        )
        assert running["max"] >= 2
    finally:
        await harness.aclose()


async def test_team_and_single_task_share_the_same_lock(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(
        tmp_path, root=root, workspace_lock_timeout_seconds=0.05
    )

    async def hook(agent, messages):
        await asyncio.sleep(0.15)

    agent = FakeAgent("deepseek", reply=json.dumps({"final": "团队"}), hook=hook)
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        team_id = harness.create_team("锁测试")
        harness.add_step(team_id, "step", "deepseek", "读", mode="read",
                         workspace=str(root.resolve()))
        await harness.command(f"/team run {team_id}")
        await asyncio.sleep(0.02)
        assert harness.workspaces._coordinator.active_locks()  # noqa: SLF001
        single = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 读')
        assert "占用" in single or "失败" in single
        await harness.wait_team(team_id)
        await harness.wait_team(team_id)
    finally:
        await harness.aclose()


def test_worker_limit_policy() -> None:
    """F15：跨进程锁未实现，任何多 worker 与布尔声明都必须被拒绝。"""
    from app.workspaces.coordinator import enforce_worker_policy

    enforce_worker_policy(max_workers=1, cross_process_lock=False, source="test")
    with pytest.raises(ValueError):
        enforce_worker_policy(max_workers=4, cross_process_lock=False, source="test")
    with pytest.raises(ValueError):
        enforce_worker_policy(max_workers=4, cross_process_lock=True, source="test")
    with pytest.raises(ValueError):
        enforce_worker_policy(max_workers=1, cross_process_lock=True, source="test")


async def test_limits_come_from_config(tmp_path) -> None:
    root = tmp_path / "工作区"
    root.mkdir()
    settings = workspace_settings(
        tmp_path, root=root,
        console_workspaces={
            "roots": [{"path": str(root), "modes": ["read"], "admin_modes": ["read"]}],
            "find": {"max_candidates": 2, "max_depth": 1, "selection_ttl_seconds": 60},
        },
    )
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        limits: WorkspaceLimits = harness.workspaces._limits  # noqa: SLF001
        assert limits.max_candidates == 2
        assert limits.max_depth == 1
        assert limits.bind_ttl_seconds == 60
    finally:
        await harness.aclose()
