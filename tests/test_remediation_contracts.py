"""整改专项契约（F01–F16 的补充面）：隔离、副作用记录、scope 矩阵、预算配置。

与 `tests/test_acceptance_probes.py`（Codex 独立复现的等价用例）互补：
- F01 的另一半：平台没有沙箱时必须**明确禁用** exec，不能退回无隔离执行；
- §5：exec 命令直接产生的文件改动必须有记录；
- F08：逐个端点的 scope 矩阵（缺 scope 一律 403）；
- §5：broker 会话的整体期限可配置。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.api.v1 import ApiIdentity, create_router
from app.backends.protocol import TaskRequest
from app.security import exec_policy as exec_module
from app.security.exec_policy import ExecPolicy, run_command
from helpers import (
    requires_platform_sandbox,
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)


# ---------- F01：没有沙箱时 exec 必须禁用 ----------


async def test_exec_is_disabled_when_platform_sandbox_missing(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(exec_module, "sandbox_available", lambda: False)
    result = await run_command(
        ["python3", "-c", "print('should not run')"],
        workspace=root, cwd=root, policy=ExecPolicy(),
    )
    assert result.exit_code == -1
    assert "隔离" in result.denied_reason
    assert "should not run" not in result.stdout


def test_isolation_status_is_reported():
    from app.security.exec_policy import isolation_status

    status = isolation_status()
    assert "sandbox-exec" in status or "禁用" in status


# ---------- §5：exec 产生的文件改动必须有记录 ----------


@requires_platform_sandbox()
async def test_exec_command_side_effects_are_recorded(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    script = [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c",
            "from pathlib import Path; Path('made-by-command.txt').write_text('x')",
        ], "timeout": 10}]}),
        json.dumps({"final": "命令已运行"}),
    ]
    harness = ConsoleHarness(settings, {"codex": ScriptedAgent("codex", script)})
    try:
        reply = await harness.command(f'/task codex --dir "{root}" --mode exec -- 生成文件')
        record = harness.task_store.list_recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, limit=1
        )[0]
        changes = harness.task_store.list_changes(task_id=record["task_id"])
        made = [item for item in changes if item["path"].endswith("made-by-command.txt")]
        assert made and made[0]["status"] == "applied"
        assert made[0]["kind"] == "create"
        assert "由命令产生" in (made[0]["error"] or "")
        # 命令确实在隔离内运行（结果里带隔离说明），且文件真的出现
        assert (root / "made-by-command.txt").is_file()
        assert "命令已运行" in reply
    finally:
        await harness.aclose()


# ---------- F08：端点 scope 矩阵 ----------


def _api_app(harness: ConsoleHarness, scopes: set[str]):
    from fastapi import FastAPI

    app = FastAPI()
    app.state.dispatcher = harness.dispatcher
    app.state.tasks = harness.tasks
    app.state.workspaces = harness.workspaces
    app.state.background = set()
    app.state.api_tokens = {
        "t": ApiIdentity(name="scoped", user_id=ALLOWED_USER,
                         group_ids=(ALLOWED_GROUP,), scopes=frozenset(scopes))
    }
    app.include_router(create_router(lambda target: target.state))
    return app


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer t"}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/v1/tasks", {"agent": "deepseek", "prompt": "x"}),
        ("POST", "/api/v1/workspaces/find", {"keyword": "x"}),
        ("POST", "/api/v1/teams", {"goal": "x"}),
    ],
)
async def test_endpoints_without_matching_scope_return_403(tmp_path, method, path, body):
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = _api_app(harness, scopes=set())  # 无任何 scope
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            response = await http.request(method, path, json=body, headers=_auth())
        assert response.status_code == 403, response.text
    finally:
        await harness.aclose()


async def test_team_actions_require_team_scope(tmp_path):
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek", reply="ok")})
    team = harness.create_team()
    harness.add_step(team, "s1", "deepseek")
    app = _api_app(harness, scopes={"workspace", "task"})  # 有 task/workspace，但没有 team
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            for path, body in (
                (f"/api/v1/teams/{team}/run", {}),
                (f"/api/v1/teams/{team}/cancel", {}),
                (f"/api/v1/teams/{team}/retry", {}),
            ):
                response = await http.post(path, json=body, headers=_auth())
                assert response.status_code == 403, (path, response.text)
            # 只读端点同样需要 team scope
            assert (await http.get(f"/api/v1/teams/{team}", headers=_auth())).status_code == 403
    finally:
        await harness.aclose()


async def test_workspaces_select_requires_workspace_scope(tmp_path):
    settings = workspace_settings(tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "target").mkdir()
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = _api_app(harness, scopes={"task"})
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            response = await http.post(
                "/api/v1/workspaces/select", json={"code": "NOPE", "index": 1}, headers=_auth()
            )
        assert response.status_code == 403
    finally:
        await harness.aclose()


# ---------- §5：broker 会话整体期限可配置 ----------


async def test_broker_session_deadline_comes_from_config(tmp_path):
    from app.config import Settings

    settings = Settings(
        _env_file=None, qq_allowed_user_ids=[ALLOWED_USER], qq_allowed_group_ids=[ALLOWED_GROUP],
        database_path=str(tmp_path / "t.db"), thinking_archive_dir="",
        console_commands={"broker": {"max_task_seconds": 42}},
        console_workspaces={"roots": []},
    )
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        assert harness.tasks._broker_deadline == 42  # noqa: SLF001
    finally:
        await harness.aclose()


async def test_broker_deadline_fails_slow_session(tmp_path):
    """整体期限到点 → 任务失败，而不是无限等待多轮模型。"""
    root = tmp_path / "ws"
    root.mkdir()
    settings = workspace_settings(
        tmp_path, root=root,
        console_commands={"broker": {"max_task_seconds": 0.05}},
    )
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "太慢"}), delay=0.2)
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 读')
        assert "失败" in reply and "期限内未完成" in reply
        record = harness.task_store.list_recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, limit=1
        )[0]
        assert record["status"] == "failed"
    finally:
        await harness.aclose()


# ---------- F04：备份目录可配置且在受控位置 ----------


async def test_backup_dir_is_configurable(tmp_path):
    from app.workspaces.configurator import build_workspace_config
    from helpers import make_settings

    custom = tmp_path / "custom-backups"
    settings = make_settings(
        tmp_path, console_workspaces={"roots": [], "backup_dir": str(custom)}
    )
    config = build_workspace_config(settings)
    assert config.backup_dir == Path(str(custom))
    assert config.backup_dir != Path(str(tmp_path / "runtime/state/backups"))


# ---------- F06：新建目标在提交前出现必须被拒绝 ----------


async def test_new_file_created_by_someone_else_is_not_overwritten(tmp_path):
    from app.workspaces.changes import ChangeSet

    target = tmp_path / "new.txt"
    changes = ChangeSet(root=tmp_path, task_id="t")
    changes.add_write(target, "planned")
    target.write_text("someone-else")          # 提交前别人创建了同名文件
    applied = changes.apply()
    assert applied[0].status == "conflict"
    assert target.read_text() == "someone-else"


async def test_delete_detects_version_drift(tmp_path):
    from app.workspaces.changes import ChangeSet

    target = tmp_path / "a.txt"
    target.write_text("v1")
    changes = ChangeSet(root=tmp_path, task_id="t")
    changes.add_delete(target)
    target.write_text("v2")                    # 提交前被外部修改
    applied = changes.apply()
    assert applied[0].status == "conflict"
    assert target.read_text() == "v2"


# ---------- A02：--dir 支持绑定目录内的相对路径 ----------


async def test_relative_dir_resolves_against_binding(tmp_path):
    root = tmp_path / "工作区"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("x", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "读到了"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        # 没有绑定时相对路径被明确拒绝
        refused = await harness.command("/task deepseek --dir sub --mode read -- 读")
        assert "需要先绑定目录" in refused
        await harness.command(f'/workspace use "{root}"')
        ok = await harness.command("/task deepseek --dir sub --mode read -- 读")
        assert "读到了" in ok
        # 越出绑定目录的相对路径仍然被拒绝
        escaped = await harness.command("/task deepseek --dir ../ --mode read -- 读")
        assert "失败" in escaped or "超出" in escaped or "不在授权根内" in escaped
    finally:
        await harness.aclose()


async def test_broker_loop_has_input_budget(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "big.txt").write_text("x" * 3000, encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root, team_max_input_chars=500)
    # 第一轮就把大文件读进来，第二轮输入必然超预算
    script = [
        json.dumps({"actions": [{"action": "read", "path": "big.txt"}]}),
        json.dumps({"final": "读完了"}),
    ]
    harness = ConsoleHarness(settings, {"deepseek": ScriptedAgent("deepseek", script)})
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 读大文件')
        assert "失败" in reply and "超过上限" in reply
    finally:
        await harness.aclose()


# ---------- F05 补充：晚到结果不得写聊天历史或声称成功 ----------


async def test_late_result_after_cancel_is_not_success_or_history(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply=json.dumps({"final": "迟到的答复"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        request = TaskRequest(
            entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek",
            prompt="读", mode="read", workspace=root, task_id="late-probe",
        )
        spec, error = harness.tasks.build_spec(request=request, workspace=root, mode="read")
        assert spec is not None, error
        # 先落库并标记为已取消，再让执行返回"成功"结果
        harness.task_store.create_task(
            entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek",
            prompt="读", mode="read", status="running", task_id="late-probe",
        )
        harness.task_store.update_status_if_active("late-probe", "cancelled")
        outcome = await harness.tasks.run(request, spec=spec, history=())
        assert outcome.ok is False and outcome.status == "cancelled"
        # 已取消的任务不再调用模型（更早收敛），也不写文件与聊天历史
        assert agent.calls == []
        record = harness.task_store.get_task("late-probe")
        assert record["status"] == "cancelled"
        assert harness.task_store.list_changes(task_id="late-probe") == []
        assert harness.context_store.recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek", max_turns=5
        ) == []
    finally:
        await harness.aclose()
