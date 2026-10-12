"""受限写入测试：`/task <agent> --dir ... --mode edit` 与公共 broker 的变更提交。

新契约（见 app/commands/registry.py、app/workspaces/broker.py、app/workspaces/changes.py）：
- 写入不再是 `/write` + `/approve`：模型返回 `propose_changes` JSON 动作，服务端逐动作
  校验授权，并在持有目录锁时提交（逐文件哈希、覆盖前备份、结果如实记录）；
- 越界 / 敏感路径 / 哈希漂移 / 超限一律拒绝，且不留副作用；
- 纯文本任务（正文里说"修改文件"）不会升级权限：没有 `--mode edit` 就没有写入口。
"""

from __future__ import annotations

from pathlib import Path

import json
import re

from app.security.acl import AclService, RootGrant
from app.security.authz import Authorizer
from app.workspaces.broker import BrokerBudget, FileBroker
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)

MODES = frozenset({"read", "edit", "exec"})


def make_broker(root, *, mode: str = "edit", budget: BrokerBudget | None = None) -> FileBroker:
    authz = Authorizer([ALLOWED_USER], [ALLOWED_GROUP])
    acl = AclService(
        authz=authz,
        grants=(RootGrant(path=root.resolve(), modes=MODES, admin_modes=MODES),),
    )
    return FileBroker(
        acl=acl, root=root.resolve(), group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
        task_id="t1", mode=mode, budget=budget,
    )


def propose(path: str, content: str) -> dict:
    return {"action": "propose_changes", "changes": [{"path": path, "content": content}]}


def final(text: str = "完成") -> str:
    return json.dumps({"final": text})


def task_id_of(reply: str) -> str:
    match = re.search(r"task ([0-9a-f]{8})", reply)
    assert match is not None, f"回复里没有 task_id：{reply}"
    return match.group(1)


# ---------- broker / 变更集本身 ----------

async def test_broker_stages_then_commits_new_file(tmp_path):
    broker = make_broker(tmp_path)
    staged = await broker.execute(propose("sub/a.md", "你好"))
    assert staged.ok is True and staged.payload["staged"][0]["kind"] == "create"
    assert not (tmp_path / "sub" / "a.md").exists(), "propose 阶段绝不落盘"

    changes = broker.commit()
    assert [change.status for change in changes] == ["applied"]
    assert changes[0].kind == "create" and changes[0].backup == ""
    assert (tmp_path / "sub" / "a.md").read_text(encoding="utf-8") == "你好"


async def test_broker_backs_up_before_overwrite(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("旧内容", encoding="utf-8")
    broker = make_broker(tmp_path)
    await broker.execute(propose("a.md", "新内容"))

    changes = broker.commit()

    assert changes[0].kind == "modify" and changes[0].status == "applied"
    assert target.read_text(encoding="utf-8") == "新内容"
    # F04：备份写入受控目录（唯一命名），不再落到目标同目录 .bak
    backup = Path(changes[0].backup)
    assert backup.is_file() and backup.read_text(encoding="utf-8") == "旧内容"
    assert not (tmp_path / "a.md.bak").exists()


async def test_broker_rejects_escape_and_sensitive_paths(tmp_path):
    outside = tmp_path.parent / "outside.md"
    broker = make_broker(tmp_path)
    for evil in ("../outside.md", "/etc/hosts", "~/x.txt", ".env", "id_rsa"):
        result = await broker.execute(propose(evil, "x"))
        assert result.ok is False, f"{evil} 必须被拒绝"
        assert result.fatal is True
    assert not outside.exists()
    assert not (tmp_path / ".env").exists()
    assert broker.commit() == []


async def test_broker_rejects_oversized_content(tmp_path):
    broker = make_broker(tmp_path, budget=BrokerBudget(max_file_bytes=10))
    result = await broker.execute(propose("a.md", "x" * 50))
    assert result.ok is False and "超过单文件上限" in result.error
    assert broker.commit() == []


async def test_broker_detects_hash_drift_after_read(tmp_path):
    """读过之后文件被外部改动：拒绝覆盖（可恢复错误，回灌给模型）。"""
    target = tmp_path / "a.txt"
    target.write_text("旧内容", encoding="utf-8")
    broker = make_broker(tmp_path)
    read = await broker.execute({"action": "read", "path": "a.txt"})
    assert read.ok is True
    target.write_text("外部改动", encoding="utf-8")

    result = await broker.execute(propose("a.txt", "新内容"))

    assert result.ok is False and result.fatal is False
    assert "哈希漂移" in result.error
    assert target.read_text(encoding="utf-8") == "外部改动"
    assert broker.commit() == []


async def test_broker_delete_needs_existing_file_and_keeps_backup(tmp_path):
    broker = make_broker(tmp_path)
    missing = await broker.execute({"action": "propose_changes", "changes": [
        {"path": "missing.txt", "delete": True}
    ]})
    assert missing.ok is False and "无法删除" in missing.error

    target = tmp_path / "a.txt"
    target.write_text("内容", encoding="utf-8")
    broker2 = make_broker(tmp_path)
    ok = await broker2.execute({"action": "propose_changes", "changes": [
        {"path": "a.txt", "delete": True}
    ]})
    assert ok.ok is True
    changes = broker2.commit()
    assert changes[0].kind == "delete" and changes[0].status == "applied"
    assert not target.exists()
    backup = Path(changes[0].backup)
    assert backup.is_file() and backup.read_text(encoding="utf-8") == "内容"
    assert not (tmp_path / "a.txt.bak").exists()


async def test_directory_target_is_refused_at_staging(tmp_path):
    """写入目标是目录：在登记阶段就被拒绝（比提交时才失败更早、更清晰）。"""
    (tmp_path / "adir").mkdir()
    broker = make_broker(tmp_path)
    staged = await broker.execute(propose("adir", "x"))
    assert staged.ok is False and "目录" in staged.error
    assert broker.commit() == []
    assert (tmp_path / "adir").is_dir()


# ---------- 命令层：编辑任务 ----------

async def test_edit_task_applies_changes_and_reports_them(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [propose("out/hello.md", "# 你好\n这是正文")]}),
        final("已创建 out/hello.md"),
    ], display_name="DeepSeek")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(
        f'/task deepseek --dir "{root}" --mode edit -- 新建 out/hello.md'
    )

    # 只有 edit 模式才有写入口，且文件真的落盘
    assert (root / "out" / "hello.md").read_text(encoding="utf-8") == "# 你好\n这是正文"
    assert "模式：edit" in out and "已创建" in out
    assert "🔧 文件变更：" in out and "新建" in out and "已应用" in out

    # /task result 给出同一份变更清单与恢复说明（可审计）
    task_id = task_id_of(out)
    result = await harness.command(f"/task result {task_id}")
    assert "文件变更" in result and "hello.md" in result
    assert "恢复：把备份文件复制回原路径" in result
    record = harness.task_store.get_task(task_id)
    assert record["status"] == "succeeded" and record["changed_files"] == 1


async def test_edit_task_overwrites_with_backup(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.md").write_text("旧", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [propose("a.md", "新内容")]}),
        final("已替换"),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode edit -- 覆盖 a.md')

    assert (root / "a.md").read_text(encoding="utf-8") == "新内容"
    backup_line = [line for line in out.splitlines() if "备份" in line]
    assert backup_line, out
    record = harness.task_store.list_recent(group_id=200, user_id=100, limit=1)[0]
    changes = harness.task_store.list_changes(task_id=record["task_id"])
    backup = Path(changes[0]["backup"])
    assert backup.is_file() and backup.read_text(encoding="utf-8") == "旧"
    assert not (root / "a.md.bak").exists()


async def test_edit_task_escape_fails_without_side_effects(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("原始", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [propose("../escape.md", "越界")]}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode edit -- 越界写入')

    assert "失败" in out and "不允许包含 .." in out
    assert not (tmp_path / "escape.md").exists()
    assert (root / "a.txt").read_text(encoding="utf-8") == "原始"
    # 权限级失败立刻停止，不再继续循环
    assert len(agent.calls) == 1


async def test_edit_task_sensitive_path_fails_without_side_effects(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [propose(".env", "API_KEY=leak")]}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode edit -- 写配置')

    assert "敏感清单" in out
    assert not (root / ".env").exists()


async def test_edit_task_requires_authorized_directory(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command('/task deepseek --dir "/etc" --mode edit -- 改文件')

    assert "不在授权根内" in out
    assert agent.calls == []


async def test_text_task_cannot_write_even_if_body_asks(tmp_path):
    """正文说"修改文件"不会升级权限：没有 --mode edit 就没有写入口。"""
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("原始", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    actions = json.dumps({"actions": [propose("a.txt", "改写")]})
    agent = FakeAgent("deepseek", reply=actions)
    harness = ConsoleHarness(settings, {"deepseek": agent})

    # --mode text 不接受 --dir：目录选择器本身就是权限声明
    out = await harness.command(f'/task deepseek --mode text --dir "{root}" -- 改文件')
    assert "不接受 --dir" in out
    assert agent.calls == []

    # 纯文本任务里模型输出的 JSON 动作只是文本，不会被执行
    out = await harness.command("/task deepseek -- 请修改 a.txt")
    assert actions in out
    assert (root / "a.txt").read_text(encoding="utf-8") == "原始"
    assert agent.options_log[-1].mode == "text"


async def test_legacy_write_and_approve_are_migration_hints(tmp_path):
    """旧 /write + /approve 只回迁移提示：不落盘、不发码、不消耗确认码。"""
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    pending = harness.confirmations.create(
        kind="action.confirm", user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, summary="占位"
    )

    write_out = await harness.command(f'/write deepseek {root}/out/hello.md\n---\n# 你好')
    approve_out = await harness.command(f"/approve {pending.code}")

    assert "旧口令" in write_out and "/task <agent> --dir" in write_out
    assert "旧口令" in approve_out and "/confirm <确认码>" in approve_out
    assert not (root / "out").exists()
    assert agent.calls == []
    # 确认码没有被旧口令消耗
    assert harness.confirmations.pending_count() == 1
    assert any("已确认" in t for t in [await harness.command(f"/confirm {pending.code}")])
