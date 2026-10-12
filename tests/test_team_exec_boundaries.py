"""第四轮补充：**真实 team 集成**验证权限边界与重试复用（T01/T02/T03 的 team 侧证据）。

Codex 第三轮验收报告 §3 指出：前一轮只以"team 复用同一个 broker"说明覆盖关系，
新增用例里没有真正跑 team 场景。本文件补齐报告 §4 明确要求的四类真实集成：

1. 父目录 exec / 子目录只读：exec 步骤不能修改子目录文件（T01）；
2. 用户被显式覆盖成 text：exec 步骤不能读写该子树（T01）；
3. protected 子树在 exec 步骤内不可读写（R02/T01）；
4. 部分读取后的 team retry：文件未变化时复用成功步骤，真正变化时报漂移拒绝复用（R06）。

全部使用临时目录、假适配器与离线传输；不调用真实模型、不发送 QQ 消息。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.workflows.models import StepStatus, TeamStatus
from helpers import (
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    requires_platform_sandbox,
    workspace_settings,
)


def _exec_script(relative: str, marker_name: str = "launch-marker.txt") -> list[str]:
    """让 exec 步骤先写一个合法标记（证明命令确实启动），再尝试改受限文件。"""
    return [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c",
            "from pathlib import Path\n"
            f"Path({marker_name!r}).write_text('launched')\n"
            f"Path({relative!r}).write_text('changed')\n",
        ], "timeout": 10}]}),
        json.dumps({"final": "命令已执行"}),
    ]


def _two_step_team(harness: ConsoleHarness, root: Path, exec_agent: str = "codex",
                   *, after: tuple[str, ...] = ("setup",)) -> str:
    team = harness.create_team("团队权限边界")
    harness.add_step(
        team, "setup", "deepseek", "预热", mode="edit", workspace=str(root.resolve()),
    )
    harness.add_step(
        team, "verify", exec_agent, "跑命令", mode="exec",
        workspace=str(root.resolve()), after=after,
    )
    return team


def _edit_observed(name: str = "deepseek", *, reply: str = "预热完成",
                   target: str = "warmup.txt") -> "ScriptedAgent":
    """edit 步骤的"有操作证据"脚本：真的提交一次文件修改，再给 final。

    V01 之后"只有 final、没有任何动作"不再算已完成步骤（status=no_change，
    team 会把该步记为 no_operation 失败），因此这类夹具必须产生真实提交回执。
    """
    return ScriptedAgent(name, [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": target, "content": "warmup"}]}]}),
        json.dumps({"final": reply}),
    ])


def _nested_settings(tmp_path, root: Path, child: Path, child_grant: dict, **extra):
    return workspace_settings(
        tmp_path, root=root,
        console_workspaces={
            "roots": [
                {"path": str(root), "modes": ["read", "edit", "exec"],
                 "admin_modes": ["read", "edit", "exec"]},
                {"path": str(child), **child_grant},
            ],
            **extra,
        },
    )


# ---------- 1) 父 exec / 子只读 ----------


@requires_platform_sandbox()
async def test_team_exec_cannot_write_readonly_child_directory(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    child = root / "readonly"
    child.mkdir()
    target = child / "memo.txt"
    target.write_text("original")

    settings = _nested_settings(
        tmp_path, root, child, {"modes": ["read"], "admin_modes": ["read"]},
    )
    harness = ConsoleHarness(settings, {
        "deepseek": _edit_observed(reply="预热完成"),
        "codex": ScriptedAgent("codex", _exec_script(str(target))),
    })
    try:
        # 夹具前提：ACL 本身拒绝该用户修改子目录
        assert not harness.acl.authorize_path(
            user_id=100, group_id=200, path=target, mode="edit",
        ).allowed
        team = _two_step_team(harness, root)
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        # 命令确实启动了（合法写入成功），但受限文件保持原样
        assert (root / "launch-marker.txt").read_text() == "launched"
        assert target.read_text() == "original"
    finally:
        await harness.aclose()


# ---------- 2) 用户被显式覆盖为 text ----------


@requires_platform_sandbox()
async def test_team_exec_cannot_use_user_downgraded_directory(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    child = root / "restricted"
    child.mkdir()
    target = child / "memo.txt"
    target.write_text("original")

    settings = _nested_settings(
        tmp_path, root, child,
        {"modes": ["read", "edit", "exec"], "admin_modes": ["read", "edit", "exec"],
         "users": {"100": ["text"]}},
    )
    harness = ConsoleHarness(settings, {
        "deepseek": _edit_observed(reply="预热完成"),
        "codex": ScriptedAgent("codex", [
            json.dumps({"actions": [{"action": "run_command", "argv": [
                "python3", "-c",
                "from pathlib import Path\n"
                "Path('launch-marker.txt').write_text('launched')\n"
                f"p = Path({str(target)!r})\n"
                "try:\n"
                "    print('CONTENT', p.read_text())\n"
                "except Exception as exc:\n"
                "    print('DENY-READ', type(exc).__name__)\n"
                "try:\n"
                "    p.write_text('changed')\n"
                "except Exception as exc:\n"
                "    print('DENY-WRITE', type(exc).__name__)\n",
            ], "timeout": 10}]}),
            json.dumps({"final": "命令已执行"}),
        ]),
    })
    try:
        assert not harness.acl.authorize_path(
            user_id=100, group_id=200, path=target, mode="read",
        ).allowed, "夹具要求用户 100 在该子树只有 text"
        team = _two_step_team(harness, root)
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        assert (root / "launch-marker.txt").read_text() == "launched"
        assert target.read_text() == "original"
        record = harness.workflow_store.get_step_run(team, 1, "verify")
        assert record.status == StepStatus.SUCCEEDED
        assert "CONTENT" not in (record.answer or ""), record.answer
    finally:
        await harness.aclose()


# ---------- 3) protected 子树在 exec 步骤内不可读写 ----------


@requires_platform_sandbox()
async def test_team_exec_cannot_touch_protected_subtree(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    protected = root / "protected"
    protected.mkdir()
    target = protected / "memo.txt"
    target.write_text("original")

    settings = workspace_settings(
        tmp_path, root=root,
        console_workspaces={
            "roots": [{"path": str(root), "modes": ["read", "edit", "exec"],
                       "admin_modes": ["read", "edit", "exec"]}],
            "protected_paths": [str(protected)],
        },
    )
    harness = ConsoleHarness(settings, {
        "deepseek": _edit_observed(reply="预热完成"),
        "codex": ScriptedAgent("codex", [
            json.dumps({"actions": [{"action": "run_command", "argv": [
                "python3", "-c",
                "from pathlib import Path\n"
                "Path('launch-marker.txt').write_text('launched')\n"
                f"p = Path({str(target)!r})\n"
                "try:\n"
                "    print('CONTENT', p.read_text())\n"
                "except Exception as exc:\n"
                "    print('DENY-READ', type(exc).__name__)\n"
                "try:\n"
                "    p.write_text('changed')\n"
                "except Exception as exc:\n"
                "    print('DENY-WRITE', type(exc).__name__)\n",
            ], "timeout": 10}]}),
            json.dumps({"final": "命令已执行"}),
        ]),
    })
    try:
        team = _two_step_team(harness, root)
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.SUCCEEDED
        assert (root / "launch-marker.txt").read_text() == "launched"
        assert target.read_text() == "original"
        record = harness.workflow_store.get_step_run(team, 1, "verify")
        assert "CONTENT" not in (record.answer or ""), record.answer
    finally:
        await harness.aclose()


# ---------- 4) 部分读取后的 retry 复用 / 漂移 ----------


def _partial_read_agents(expected_bytes: int = 4) -> dict:
    reader = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt",
                                 "max_bytes": expected_bytes}]}),
        json.dumps({"final": "已读取片段"}),
    ])
    failing = FakeAgent("codex", error="network")
    return {"deepseek": reader, "codex": failing}


async def test_team_partial_read_step_is_reused_when_unchanged(tmp_path) -> None:
    """部分读取（截断）后的 team retry：文件没变就必须能复用，不能误报漂移。"""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("abcdefghij", encoding="utf-8")
    harness = ConsoleHarness(
        workspace_settings(tmp_path, root=root), _partial_read_agents(),
    )
    try:
        team = harness.create_team("部分读取复用")
        harness.add_step(team, "peek", "deepseek", "看片段", mode="read",
                         workspace=str(root.resolve()))
        harness.add_step(team, "later", "codex", "后续", mode="read",
                         workspace=str(root.resolve()), after=("peek",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        record = harness.workflow_store.get_step_run(team, 1, "peek")
        assert record.status == StepStatus.SUCCEEDED
        # 指纹必须是全文件版本，而不是 4 字节片段的摘要
        from app.workspaces.changes import file_hash

        assert record.input_fingerprint[str(root.resolve() / "a.txt")] == file_hash(root / "a.txt")
        retry = await harness.command(f"/team retry {team}")
        assert "复用成功步骤：peek" in retry, retry
    finally:
        await harness.aclose()


async def test_team_partial_read_step_requires_drift_handling_when_changed(tmp_path) -> None:
    """部分读取之后文件真的变了：retry 必须拒绝复用并要求处理漂移。"""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("abcdefghij", encoding="utf-8")
    harness = ConsoleHarness(
        workspace_settings(tmp_path, root=root), _partial_read_agents(),
    )
    try:
        team = harness.create_team("部分读取漂移")
        harness.add_step(team, "peek", "deepseek", "看片段", mode="read",
                         workspace=str(root.resolve()))
        harness.add_step(team, "later", "codex", "后续", mode="read",
                         workspace=str(root.resolve()), after=("peek",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == TeamStatus.FAILED
        (root / "a.txt").write_text("abcdefghij-external", encoding="utf-8")
        retry = await harness.command(f"/team retry {team}")
        assert "漂移" in retry, retry
        assert harness.workflow_store.get_workflow(team).round == 1
    finally:
        await harness.aclose()
