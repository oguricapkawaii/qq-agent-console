"""A05-A09：公共文件 broker 的读写、只读强约束、冲突与路径边界、exec 隔离。

这些测试检查**真实副作用**（文件内容与哈希），不只看 tool_log。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.security.acl import AclService
from app.security.authz import Authorizer
from app.security.exec_policy import ExecPolicy
from app.workspaces.broker import BrokerBudget, FileBroker
from app.workspaces.changes import file_hash
from helpers import (
    requires_platform_sandbox,
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)


def _make_acl(root: Path, *, modes=("read", "edit", "exec"), user_id: int = ALLOWED_USER) -> AclService:
    from app.security.acl import RootGrant

    authz = Authorizer([user_id], [ALLOWED_GROUP])
    return AclService(
        authz=authz,
        grants=(RootGrant(path=root.resolve(), modes=frozenset(modes),
                          admin_modes=frozenset(modes), users={}, label="test"),
                ),
        protected_roots=(),
    )


def _broker(root: Path, mode: str, *, user_id: int = ALLOWED_USER, policy=None) -> FileBroker:
    return FileBroker(
        acl=_make_acl(root, user_id=user_id), root=root.resolve(), group_id=ALLOWED_GROUP,
        user_id=user_id, task_id="t1", mode=mode,
        budget=BrokerBudget(max_actions=20, max_rounds=4), exec_policy=policy,
    )


# ---------- A05 真正统一文件操作 ----------


async def test_broker_read_returns_content_and_hash(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("内容\n", encoding="utf-8")
    broker = _broker(root, "read")
    result = await broker.execute({"action": "read", "path": "a.txt"})
    assert result.ok
    assert result.payload["text"] == "内容\n"
    assert result.payload["sha256"] == file_hash(root / "a.txt")


async def test_broker_edit_writes_real_changes(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("旧\n", encoding="utf-8")
    broker = _broker(root, "edit")
    staged = await broker.execute({
        "action": "propose_changes",
        "changes": [{"path": "a.txt", "content": "新\n"}],
    })
    assert staged.ok and staged.payload["staged"][0]["diff"]
    assert target.read_text(encoding="utf-8") == "旧\n"  # 未提交前不落盘
    changes = broker.commit()
    assert [change.status for change in changes] == ["applied"]
    assert target.read_text(encoding="utf-8") == "新\n"
    # F04：备份落在受控目录（不再写目标同目录 .bak）
    backup = changes[0].backup
    assert backup and Path(backup).read_text(encoding="utf-8") == "旧\n"
    assert not (root / "a.txt.bak").exists()


async def test_broker_creates_new_files_and_reports_hash_drift(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "new.txt"
    target.write_text("v1", encoding="utf-8")
    broker = _broker(root, "edit")
    read = await broker.execute({"action": "read", "path": "new.txt"})
    assert read.ok
    # 外部修改导致哈希漂移 → 拒绝覆盖
    target.write_text("v2-external", encoding="utf-8")
    refused = await broker.execute({
        "action": "propose_changes",
        "changes": [{"path": "new.txt", "content": "v3"}],
    })
    assert refused.ok is False
    assert "漂移" in refused.error or "哈希" in refused.error
    assert target.read_text(encoding="utf-8") == "v2-external"


async def test_every_backend_can_read_and_edit_through_broker(tmp_path) -> None:
    """A05：不同 Agent（含纯文本 API 后端）使用同一语法完成读取与修改。"""
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "README.md"
    target.write_text("旧内容\n", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)

    def script(name: str) -> list[str]:
        return [
            json.dumps({"actions": [{"action": "read", "path": "README.md"}]}),
            json.dumps({"actions": [{"action": "propose_changes", "changes": [
                {"path": "README.md", "content": f"由 {name} 修改\n"}]}]}),
            json.dumps({"final": f"{name} 完成"}),
        ]

    agents = {
        "deepseek": ScriptedAgent("deepseek", script("deepseek")),
        "zcode": ScriptedAgent("zcode", script("zcode")),
        "gpt": ScriptedAgent("gpt", script("gpt")),
    }
    harness = ConsoleHarness(settings, agents)
    try:
        for name in ("deepseek", "zcode", "gpt"):
            reply = await harness.command(
                f'/task {name} --dir "{root}" --mode edit -- 更新 README'
            )
            assert "完成" in reply, reply
            assert target.read_text(encoding="utf-8") == f"由 {name} 修改\n"
            target.write_text("旧内容\n", encoding="utf-8")
        # 每个后端都真的读了文件（不是靠文字答复冒充完成）
        for agent in agents.values():
            assert any("README.md" in call[-1].content for call in agent.calls)
    finally:
        await harness.aclose()


# ---------- A06 只读强约束 ----------


async def test_read_mode_cannot_write_no_side_effect(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("原始\n", encoding="utf-8")
    before = file_hash(target)
    script = [
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": "被改\n"}]}]}),
        json.dumps({"final": "我改了"}),
    ]
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": ScriptedAgent("deepseek", script)})
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 修改 a.txt')
        assert "失败" in reply
        assert file_hash(target) == before
        assert target.read_text(encoding="utf-8") == "原始\n"
        assert not (root / "a.txt.bak").exists()
    finally:
        await harness.aclose()


async def test_read_mode_cannot_run_commands(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    marker = root / "marker.txt"
    script = [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c", f"open({str(marker)!r},'w').write('x')"]}]}),
        json.dumps({"final": "done"}),
    ]
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": ScriptedAgent("deepseek", script)})
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 跑命令')
        assert "失败" in reply
        assert not marker.exists()
    finally:
        await harness.aclose()


async def test_text_mode_has_no_tools_at_all(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    agent = FakeAgent("deepseek", reply="纯文本答复")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command("/task deepseek -- 只聊天")
        assert "纯文本答复" in reply
        options = agent.options_log[-1]
        assert options.mode == "text" and options.directory is None
        assert options.workspace_tools is False
    finally:
        await harness.aclose()


async def test_unknown_broker_action_fails_task(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    script = [
        json.dumps({"actions": [{"action": "shell", "command": "rm -rf /"}]}),
        json.dumps({"final": "done"}),
    ]
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": ScriptedAgent("deepseek", script)})
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode exec -- 执行')
        assert "失败" in reply
    finally:
        await harness.aclose()


async def test_bad_json_fails_without_execution(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    # 坏输出给两次：broker 会先做一次**无副作用**的协议修复重试，之后才判定失败
    harness = ConsoleHarness(
        settings, {"deepseek": ScriptedAgent(
            "deepseek", ["我建议你把文件改成这样…", "又忘了 JSON，还是自然语言"]
        )}
    )
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode edit -- 修改')
        assert "失败" in reply
        assert "JSON" in reply
    finally:
        await harness.aclose()


# ---------- A07 修改与冲突 ----------


async def test_partial_failure_records_per_file_status(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    ok_file = root / "ok.txt"
    ok_file.write_text("old", encoding="utf-8")
    protected = root / ".env"
    protected.write_text("SECRET=1", encoding="utf-8")
    broker = _broker(root, "edit")
    # 第二个文件命中敏感清单 → 整个 propose 被拒绝（fatal），第一个也不应落盘
    result = await broker.execute({
        "action": "propose_changes",
        "changes": [
            {"path": "ok.txt", "content": "new"},
            {"path": ".env", "content": "SECRET=2"},
        ],
    })
    assert result.ok is False and result.fatal is True
    assert ok_file.read_text(encoding="utf-8") == "old"
    assert protected.read_text(encoding="utf-8") == "SECRET=1"


async def test_change_set_detects_plan_time_and_apply_time_conflicts(tmp_path) -> None:
    from app.workspaces.changes import ChangeError, ChangeSet, file_hash

    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("v1", encoding="utf-8")
    # 计划时就发现哈希漂移 → 直接拒绝（不进入待提交列表）
    changes = ChangeSet(root=root, task_id="t", max_bytes=1024)
    with pytest.raises(ChangeError):
        changes.add_write(target, "v2", expected_hash="deadbeef")
    # 计划时正确、提交前被外部修改 → 提交阶段标记冲突且不写盘
    changes = ChangeSet(root=root, task_id="t", max_bytes=1024)
    changes.add_write(target, "v2", expected_hash=file_hash(target))
    target.write_text("v1-external", encoding="utf-8")
    applied = changes.apply()
    assert applied[0].status in {"failed", "conflict"}
    assert target.read_text(encoding="utf-8") == "v1-external"


# ---------- A09 路径边界 ----------


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "sub/../../outside.txt", "/etc/passwd", "~/.ssh/id_rsa", ".env", "a.pem"],
)
async def test_broker_rejects_dangerous_paths(tmp_path, path: str) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "sub").mkdir()
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    broker = _broker(root, "read")
    result = await broker.execute({"action": "read", "path": path})
    assert result.ok is False


async def test_symlink_escape_is_rejected(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:  # pragma: no cover - 平台不支持符号链接
        pytest.skip("当前平台不支持符号链接")
    broker = _broker(root, "read")
    result = await broker.execute({"action": "read", "path": "link.txt"})
    assert result.ok is False
    assert "超出" in result.error or "敏感" in result.error


async def test_adjacent_same_prefix_directory_is_outside(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sibling = tmp_path / "proj-backup"
    sibling.mkdir()
    (sibling / "x.txt").write_text("sibling", encoding="utf-8")
    broker = _broker(root, "read")
    refused = await broker.execute({"action": "read", "path": "../proj-backup/x.txt"})
    assert refused.ok is False
    # 兄弟目录不在授权根内：即使 acl 判定也不通过
    acl = _make_acl(root)
    decision = acl.authorize_path(
        user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, path=sibling, mode="read"
    )
    assert decision.allowed is False


async def test_acl_handles_parent_and_child_roots(tmp_path) -> None:
    from app.security.acl import AclService, RootGrant

    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    authz = Authorizer([ALLOWED_USER], [ALLOWED_GROUP])
    acl = AclService(
        authz=authz,
        grants=(
            RootGrant(path=parent.resolve(), modes=frozenset({"read"}),
                      admin_modes=frozenset({"read"})),
            RootGrant(path=child.resolve(), modes=frozenset({"read", "edit"}),
                      admin_modes=frozenset({"read", "edit"})),
        ),
        protected_roots=(),
    )
    assert acl.authorize_path(
        user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, path=child, mode="edit"
    ).allowed
    # 最具体的授权根生效：父目录本身只有 read
    assert not acl.authorize_path(
        user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, path=parent, mode="edit"
    ).allowed
    assert not acl.authorize_path(
        user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, path=outside, mode="read"
    ).allowed


async def test_binary_and_oversized_reads_are_refused(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "bin.dat").write_bytes(b"\x00\x01\x02")
    big = root / "big.txt"
    big.write_text("x" * 5000, encoding="utf-8")
    broker = FileBroker(
        acl=_make_acl(root), root=root.resolve(), group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
        task_id="t", mode="read", budget=BrokerBudget(max_file_bytes=1024),
    )
    binary = await broker.execute({"action": "read", "path": "bin.dat"})
    assert binary.ok is False and "二进制" in binary.error
    limited = await broker.execute({"action": "read", "path": "big.txt"})
    assert limited.ok and limited.payload["bytes"] == 1024
    assert limited.payload["truncated"] is True


async def test_protected_robot_directory_is_refused(tmp_path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    acl_dir = project_root / "app"
    authz = Authorizer([ALLOWED_USER], [ALLOWED_GROUP])
    from app.security.acl import RootGrant

    acl = AclService(
        authz=authz,
        grants=(
            RootGrant(path=project_root.resolve(), modes=frozenset({"read", "edit"}),
                      admin_modes=frozenset({"read", "edit"})),
        ),
        protected_roots=(project_root.resolve(),),
    )
    decision = acl.authorize_path(
        user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, path=acl_dir, mode="read"
    )
    assert decision.allowed is False
    assert "受保护" in decision.reason


# ---------- A08 exec 隔离 ----------


@requires_platform_sandbox()
async def test_exec_runs_allowed_command_and_captures_output(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    script = [
        json.dumps({"actions": [{"action": "run_command", "argv": [
            "python3", "-c", "print('hello-exec')"], "timeout": 10}]}),
        json.dumps({"final": "命令已运行"}),
    ]
    harness = ConsoleHarness(settings, {"codex": ScriptedAgent("codex", script)})
    try:
        reply = await harness.command(f'/task codex --dir "{root}" --mode exec -- 运行测试')
        assert "命令已运行" in reply
        assert "hello-exec" in reply
    finally:
        await harness.aclose()


@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "ls"],
        ["curl", "https://example.com"],
        ["rm", "-rf", "/"],
        ["sh", "-c", "echo hi"],
        ["cat", ".env"],
        ["bash", "-c", "echo hi"],
    ],
)

async def test_exec_policy_denies_dangerous_commands(tmp_path, argv) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    policy = ExecPolicy()
    from app.security.exec_policy import run_command

    result = await run_command(argv, workspace=root.resolve(), cwd=root.resolve(), policy=policy)
    assert result.denied_reason, argv
    assert result.exit_code == -1


async def test_exec_policy_denies_out_of_workspace_cwd(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    from app.security.exec_policy import ExecDenied, run_command

    result = await run_command(
        ["ls"], workspace=root.resolve(), cwd=outside.resolve(), policy=ExecPolicy()
    )
    assert "工作目录超出" in result.denied_reason or result.denied_reason
    with pytest.raises(ExecDenied):
        ExecPolicy().check(["ls"], workspace=root.resolve(), cwd=outside.resolve())


@requires_platform_sandbox()
async def test_exec_policy_timeout_kills_process_group(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    from app.security.exec_policy import run_command

    result = await run_command(
        ["python3", "-c", "import time; time.sleep(30)"],
        workspace=root.resolve(), cwd=root.resolve(), policy=ExecPolicy(max_timeout=1),
        timeout=1,
    )
    assert result.timed_out is True


async def test_backend_without_exec_capability_fails_explicitly(tmp_path) -> None:
    """不支持 exec 的后端必须明确失败，不得偷偷换后端或降级。"""
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="x", broker_modes=frozenset({"read", "edit"}))
    harness = ConsoleHarness(settings, {"deepseek": agent})
    # 手工把该后端的能力限制为 read/edit（模拟"未配置命令策略"的后端）
    harness.backends.get("deepseek").descriptor = type(
        harness.backends.get("deepseek").descriptor
    )(
        id="deepseek", agent="deepseek", adapter_type="fake", display_name="DEEPSEEK",
        enabled=True, configured=True, models=("fake-deepseek",),
        model_efforts={"fake-deepseek": ()}, default_model="fake-deepseek",
        native_modes=frozenset(), broker_modes=frozenset({"read", "edit"}),
        isolation="broker", exec_policy=False,
    )
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode exec -- 运行')
        assert "capability_not_supported" in reply or "不支持" in reply
        assert agent.calls == []
    finally:
        await harness.aclose()


# ---------- 原生工具（仅显式开启且隔离可验证时） ----------


async def test_native_tools_opt_in_uses_sandbox_and_records_changes(tmp_path) -> None:
    """native_tools=True 时走原生路径，并用有界快照记录真实变更。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("old", encoding="utf-8")

    async def hook(agent, messages):
        # 模拟"模型用原生工具改了文件"
        (root / "a.txt").write_text("new", encoding="utf-8")
        (root / "created.txt").write_text("x", encoding="utf-8")

    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent(
        "codex", reply="原生完成", hook=hook,
        native_modes=frozenset({"read", "edit"}), isolation="sandbox",
        runtime_options={"native_tools": True},
    )
    harness = ConsoleHarness(settings, {"codex": agent})
    try:
        reply = await harness.command(f'/task codex --dir "{root}" --mode edit -- 修改')
        assert "原生完成" in reply
        assert "修改 a.txt" in reply and "新建 created.txt" in reply
        options = agent.options_log[-1]
        assert options.mode == "edit"
        assert options.directory == str(root.resolve())
    finally:
        await harness.aclose()


async def test_native_tools_disabled_by_default(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent(
        "codex", reply=json.dumps({"final": "broker 完成"}),
        native_modes=frozenset({"read", "edit"}), isolation="sandbox",
        runtime_options={},
    )
    harness = ConsoleHarness(settings, {"codex": agent})
    try:
        reply = await harness.command(f'/task codex --dir "{root}" --mode edit -- 修改')
        assert "broker 完成" in reply
        # broker 路径下 adapter 收到的是协议提示词，而不是自由对话
        assert "propose_changes" in agent.calls[-1][0].content
    finally:
        await harness.aclose()


async def test_protocol_repair_retries_once_without_side_effects(tmp_path) -> None:
    """协议修复重试：首次坏 JSON → 提示纠正 → 第二次合法 → 任务成功且只执行一次动作。

    关键点：修复重试发生在**执行任何动作之前**，因此不会重复副作用；
    并且只重试一次（再次失败即如实报 bad_actions，见 test_bad_json_fails_without_execution）。
    """
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("v1", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        "我想改文件：{\"actions\": [...]}（这是解释性文字，不是纯 JSON）",
        json.dumps({"actions": [{"action": "propose_changes", "changes": [
            {"path": "a.txt", "content": "v2"}]}]}),
        json.dumps({"final": "已修改为 v2"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        reply = await harness.command(f'/task deepseek --dir "{root}" --mode edit -- 改 a.txt')
        assert "已修改为 v2" in reply, reply
        assert target.read_text(encoding="utf-8") == "v2"
        assert len(agent.calls) == 3, "应当只多出一次格式纠正轮次"
    finally:
        await harness.aclose()
