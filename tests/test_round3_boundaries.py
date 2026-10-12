"""第三轮边界复现：R01–R06 的正向与负向用例（真实副作用 + 真实 Seatbelt）。

来源：`outputs/统一控制台Codex复验报告.md` 的 R01–R06 与"下一次放行条件"第 2 条
（补充合法 read/edit/exec 正向用例，确保修复权限边界后仍能正常使用）。
这里使用**临时目录与伪造标记**，绝不读取真实凭据、不调用真实模型、不发送 QQ 消息。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.security.exec_policy import ExecPolicy, run_command
from app.security.safeopen import SafeRoot, SymlinkRejected
from app.workspaces.broker import BrokerBudget, FileBroker
from app.workspaces.changes import ChangeSet, file_hash
from helpers import (  # noqa: E402
    ConsoleHarness,
    FakeAgent,
    requires_platform_sandbox,
    workspace_settings,
)


def make_broker(harness, root: Path, mode: str = "read", **kwargs) -> FileBroker:
    return FileBroker(
        acl=harness.acl, root=root, group_id=200, user_id=100,
        task_id="round3", mode=mode, exec_policy=ExecPolicy(), **kwargs,
    )


def real(path: Path) -> Path:
    """规则与断言一律用真实路径（macOS 上 /tmp 是 /private/tmp 的符号链接）。"""
    return Path(os.path.realpath(str(path)))


def fixture_workspace(tmp_path, name: str = "workspace") -> Path:
    root = tmp_path / name
    root.mkdir()
    return real(root)


def nested_acl_settings(tmp_path, root: Path, *, private_modes=("text",)):
    """父目录 read、子目录更严格（默认只有 text）的 ACL 配置。"""
    return workspace_settings(
        real(tmp_path), root=root,
        console_workspaces={"roots": [
            {"path": str(root), "modes": ["read", "edit", "exec"],
             "admin_modes": ["read", "edit", "exec"]},
            {"path": str(root / "private"), "modes": list(private_modes),
             "admin_modes": list(private_modes)},
        ]},
    )


# ---------- R01 exec 读取隔离 ----------


@requires_platform_sandbox()
@pytest.mark.parametrize("executable", [sys.executable, "python3"])
async def test_exec_cannot_read_sensitive_files_anywhere(tmp_path, executable) -> None:
    """`.env*`/密钥在**任意位置**（同级、上级、嵌套）都读不到（真实沙箱）。"""
    root = fixture_workspace(tmp_path)
    nested = root / "sub" / "deep"
    nested.mkdir(parents=True)
    sibling = real(tmp_path) / ".env"
    sibling.write_text("SYNTHETIC_SIBLING_ENV")
    parent_env = real(tmp_path.parent) / f".env.{os.getpid()}"
    parent_env.write_text("SYNTHETIC_PARENT_ENV")
    (nested / ".env.local").write_text("SYNTHETIC_NESTED_ENV")
    (root / "server.pem").write_text("SYNTHETIC_PEM")
    (root / "id_rsa").write_text("SYNTHETIC_KEY")
    probes = [sibling, parent_env, nested / ".env.local", root / "server.pem", root / "id_rsa"]
    code = (
        "from pathlib import Path\n"
        f"for target in {[str(item) for item in probes]!r}:\n"
        "    try:\n"
        "        print('LEAK', target, Path(target).read_text())\n"
        "    except Exception as exc:\n"
        "        print('DENIED', target, type(exc).__name__)\n"
    )
    try:
        result = await run_command(
            [executable, "-c", code], workspace=root, cwd=root, policy=ExecPolicy(),
        )
    finally:
        parent_env.unlink(missing_ok=True)
    assert "LEAK" not in result.stdout, result
    assert "SYNTHETIC" not in result.stdout, result


@requires_platform_sandbox()
async def test_exec_can_still_read_and_write_workspace(tmp_path) -> None:
    """收敛读取白名单后，工作区内的正常读写必须仍然可用（不能靠禁用功能过关）。"""
    root = fixture_workspace(tmp_path)
    nested = root / "pkg"
    nested.mkdir()
    (root / "input.txt").write_text("hello-workspace")
    (nested / "mod.py").write_text("VALUE = 41\n")
    code = (
        "from pathlib import Path\n"
        "import mod\n".replace("import mod", f"import sys; sys.path.insert(0, {str(nested)!r}); import mod")
        + "print('read', Path('input.txt').read_text())\n"
        + "Path('out.txt').write_text('written')\n"
        + "Path('pkg/tmp.txt').write_text('nested-written')\n"
        + "print('module', mod.VALUE)\n"
    )
    result = await run_command(
        [sys.executable, "-c", code], workspace=root, cwd=root, policy=ExecPolicy(),
    )
    assert result.exit_code == 0, result
    assert "read hello-workspace" in result.stdout
    assert "module 41" in result.stdout
    assert (root / "out.txt").read_text() == "written"
    assert (nested / "tmp.txt").read_text() == "nested-written"


@requires_platform_sandbox()
async def test_exec_can_read_interpreter_and_virtualenv(tmp_path) -> None:
    """解释器自身与 venv 必须可读可执行，否则沙箱内根本跑不起来。"""
    root = fixture_workspace(tmp_path)
    result = await run_command(
        [sys.executable, "-c", "import json, sys; print('json-ok', sys.version_info[0])"],
        workspace=root, cwd=root, policy=ExecPolicy(),
    )
    assert result.exit_code == 0 and "json-ok" in result.stdout, result


# ---------- R02 exec 受保护路径（含工作区内部） ----------


def test_sensitive_list_and_sandbox_patterns_agree() -> None:
    """守门用例：broker 的敏感判定与 exec 沙箱正则必须覆盖同一份清单。

    两者一旦脱节，就会出现"broker 拒绝、exec 却能读"的边界缝隙（R01 的根因之一）。
    新增敏感名时，本用例会失败，提醒同时更新 `app/security/paths.py` 的清单。
    """
    import re

    from app.security.paths import is_sensitive, seatbelt_patterns

    patterns = seatbelt_patterns()
    names = [
        ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "credentials",
        "credentials.json", "auth.json", "secrets.yaml", "secrets.yml", ".netrc",
        ".pgpass", ".git-credentials", "session.db", "server.pem", "a.key", "b.p12",
        "c.pfx", "d.jks", "e.keystore",
    ]
    for name in names:
        path = Path("/x/y") / name
        assert is_sensitive(path), f"broker 敏感清单漏掉 {name}"
        assert any(re.search(item, str(path)) for item in patterns), (
            f"沙箱正则漏掉 {name}；请更新 app/security/paths.py::seatbelt_patterns"
        )
    for directory in (".ssh", ".aws", ".gnupg", ".codex", ".zcode", ".config", ".git"):
        path = f"/x/{directory}/config"
        assert is_sensitive(Path(path)), f"broker 敏感清单漏掉目录 {directory}"
        assert any(re.search(item, path) for item in patterns), (
            f"沙箱正则漏掉敏感目录 {directory}"
        )


def test_project_root_entries_are_denied_by_default() -> None:
    """守门用例：机器人项目自身的每个根条目都必须落在 exec 的拒绝侧。

    解释器可能装在项目内的 `.venv`，因此不能让"整个项目目录"一条 deny 了事；
    实现改为逐项拒绝 + 白名单（`app/security/sandbox.py::_PROJECT_VENV_NAMES`）。
    本用例保证：一旦项目根出现新条目（例如新的凭据文件、新的数据目录），
    验证者必须显式判断它是否该被放行，而不是静默变成 exec 可读。
    """
    from app.security.sandbox import _PROJECT_VENV_NAMES, deny_read_paths

    project = Path(__file__).resolve().parents[1]
    denies = set(deny_read_paths(workspace=project, protected=()))
    for entry in project.iterdir():
        if entry.name in _PROJECT_VENV_NAMES:
            continue
        assert real(entry) in denies, (
            f"项目根条目 {entry.name} 没有进入 exec 读取拒绝清单；"
            "请在 app/security/sandbox.py 的逐项逻辑里显式处理"
        )
    for name in (".env", "config.example.yaml", "README.md"):
        target = project / name
        if target.exists():
            assert real(target) in denies, name


@requires_platform_sandbox()
async def test_exec_cannot_read_robot_project_files(tmp_path) -> None:
    """exec 读不到机器人自身的 .env/配置/源码（即使在授权目录内运行）。"""
    root = fixture_workspace(tmp_path)
    project = Path(__file__).resolve().parents[1]
    probes = [
        project / ".env", project / "config.example.yaml", project / "app" / "main.py",
    ]
    existing = [item for item in probes if item.exists()]
    assert existing, "夹具需要至少一个项目文件存在"
    code = (
        "from pathlib import Path\n"
        f"for target in {[str(item) for item in existing]!r}:\n"
        "    try:\n"
        "        print('LEAK', target, Path(target).read_text()[:20])\n"
        "    except Exception as exc:\n"
        "        print('DENIED', target, type(exc).__name__)\n"
    )
    result = await run_command(
        [sys.executable, "-c", code], workspace=root, cwd=root, policy=ExecPolicy(),
    )
    assert "LEAK" not in result.stdout, result


@requires_platform_sandbox()
async def test_exec_cannot_read_or_write_nested_protected_directory(tmp_path) -> None:
    root = fixture_workspace(tmp_path)
    protected = root / "protected"
    protected.mkdir()
    target = protected / "memo.txt"
    target.write_text("original")
    code = (
        "from pathlib import Path\n"
        f"p = Path({str(target)!r})\n"
        "try:\n"
        "    print('READ', p.read_text())\n"
        "except Exception as exc:\n"
        "    print('DENY-READ', type(exc).__name__)\n"
        "try:\n"
        "    p.write_text('changed')\n"
        "    print('WROTE')\n"
        "except Exception as exc:\n"
        "    print('DENY-WRITE', type(exc).__name__)\n"
    )
    result = await run_command(
        [sys.executable, "-c", code], workspace=root, cwd=root,
        policy=ExecPolicy(), protected=(protected,),
    )
    assert target.read_text() == "original", "exec overrode nested protected path"
    assert "READ original" not in result.stdout, result
    assert "WROTE" not in result.stdout, result


@requires_platform_sandbox()
async def test_exec_still_writes_normal_workspace_file(tmp_path) -> None:
    """受保护目录之外的文件必须仍能合法修改（保护不能变成全面禁写）。"""
    root = fixture_workspace(tmp_path)
    protected = root / "protected"
    protected.mkdir()
    target = root / "normal.txt"
    target.write_text("v1")
    code = f"from pathlib import Path; Path({str(target)!r}).write_text('v2'); print('ok')"
    result = await run_command(
        [sys.executable, "-c", code], workspace=root, cwd=root,
        policy=ExecPolicy(), protected=(protected,),
    )
    assert result.exit_code == 0, result
    assert target.read_text() == "v2"


# ---------- R03 每个后代路径都尊重最具体 ACL ----------


async def test_list_and_search_respect_nested_acl(tmp_path) -> None:
    root = fixture_workspace(tmp_path)
    private = root / "private"
    private.mkdir()
    (private / "memo.txt").write_text("SYNTHETIC_ACL_MARKER")
    (root / "ok.txt").write_text("SYNTHETIC_ACL_MARKER")
    harness = ConsoleHarness(nested_acl_settings(tmp_path, root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root)
        assert not (await broker.execute({"action": "read", "path": "private/memo.txt"})).ok
        found = await broker.execute({"action": "search", "query": "SYNTHETIC_ACL_MARKER"})
        assert [hit["path"] for hit in found.payload["hits"]] == ["ok.txt"], found.payload
        listed = await broker.execute({"action": "list", "path": ".", "depth": 2})
        names = {entry["path"] for entry in listed.payload["entries"]}
        assert "ok.txt" in names and "private/memo.txt" not in names
        # 更具体的 text-only 授权：连 list 都必须明确拒绝（不是返回空目录）
        deep = await broker.execute({"action": "list", "path": "private"})
        assert not deep.ok and deep.fatal, deep
    finally:
        await harness.aclose()


async def test_search_honours_revoked_acl_at_open_time(tmp_path) -> None:
    """实时撤权：同一个文件在 search 打开前被撤权就必须读不到。"""
    root = fixture_workspace(tmp_path)
    (root / "memo.txt").write_text("SYNTHETIC_REVOKE_MARKER")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root)
        seen: list[str] = []
        original = broker._authorize

        def revoke_after_first(path, *, write):
            seen.append(str(path))
            if len(seen) > 1:
                # 第二次（逐文件复查）时模拟"用户刚撤权"
                from app.workspaces.broker import BrokerFatal

                raise BrokerFatal("你的授权已被撤销。")
            return original(path, write=write)

        with patch.object(broker, "_authorize", revoke_after_first):
            found = await broker.execute({"action": "search", "query": "SYNTHETIC_REVOKE_MARKER"})
        assert not found.payload.get("hits"), found.payload
    finally:
        await harness.aclose()


@requires_platform_sandbox()
async def test_exec_respects_nested_restricted_directory(tmp_path) -> None:
    """exec 的根授权不能覆盖内部更严格的子目录 ACL（R02 的同一条规则）。"""
    root = fixture_workspace(tmp_path)
    private = root / "private"
    private.mkdir()
    (private / "memo.txt").write_text("SYNTHETIC_EXEC_ACL_MARKER")
    harness = ConsoleHarness(nested_acl_settings(tmp_path, root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root, "exec")
        code = (
            "from pathlib import Path\n"
            f"p = Path({str(private / 'memo.txt')!r})\n"
            "try:\n"
            "    print('LEAK', p.read_text())\n"
            "except Exception as exc:\n"
            "    print('DENIED', type(exc).__name__)\n"
        )
        result = await broker.execute({"action": "run_command", "argv": [sys.executable, "-c", code]})
        assert result.ok, result.error
        assert "LEAK" not in result.payload["stdout"], result.payload
    finally:
        await harness.aclose()


# ---------- R04 备份父链 ----------


def test_backup_rejects_parent_directory_symlink(tmp_path) -> None:
    root = fixture_workspace(tmp_path)
    outside = real(tmp_path) / "outside"
    outside.mkdir()
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    (backup_root / "round2").symlink_to(outside, target_is_directory=True)
    target = root / "a.txt"
    target.write_text("original")
    changes = ChangeSet(root=root, task_id="round2", backup_root=backup_root)
    try:
        changes.add_write(target, "changed")
        applied = changes.apply()
    finally:
        changes.close()
    assert not list(outside.iterdir()), "backup created outside controlled backup root"
    assert applied[0].status == "failed", applied[0]
    assert "备份" in applied[0].error
    assert target.read_text() == "original", "unsafe backup must cancel the overwrite"


def test_backup_still_works_with_safe_chain(tmp_path) -> None:
    """正向：父链安全时备份照常生成且不覆盖已有备份。"""
    root = fixture_workspace(tmp_path)
    (root / "nested").mkdir(parents=True)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    target = root / "nested" / "a.txt"
    target.write_text("v1")
    changes = ChangeSet(root=root, task_id="t-ok", backup_root=backup_root)
    try:
        changes.add_write(target, "v2")
        applied = changes.apply()
    finally:
        changes.close()
    assert applied[0].status == "applied", applied[0]
    assert target.read_text() == "v2"
    backup = Path(applied[0].backup)
    assert backup.is_file() and backup.read_text() == "v1"
    assert backup.parent == backup_root / "t-ok"


def test_default_backup_stays_inside_workspace(tmp_path) -> None:
    """不配置 backup_root 时，备份也只落在工作区内。"""
    root = fixture_workspace(tmp_path)
    target = root / "a.txt"
    target.write_text("v1")
    changes = ChangeSet(root=root, task_id="t-default")
    try:
        changes.add_write(target, "v2")
        applied = changes.apply()
    finally:
        changes.close()
    assert applied[0].status == "applied", applied[0]
    backup = Path(applied[0].backup)
    assert backup.is_file() and backup.read_text() == "v1"
    assert root in backup.parents


def test_repeated_commits_keep_making_backups(tmp_path) -> None:
    """同一任务反复改同一文件（team retry）时备份必须一直成功，不能互相挤占。"""
    root = fixture_workspace(tmp_path)
    nested = root / "sub"
    nested.mkdir()
    target = root / "a.txt"
    nested_target = nested / "a.txt"
    target.write_text("v0")
    nested_target.write_text("s0")
    for round_number in range(5):
        changes = ChangeSet(root=root, task_id="retry", backup_root=tmp_path / "bk")
        try:
            changes.add_write(target, f"v{round_number}")
            changes.add_write(nested_target, f"s{round_number}")
            applied = changes.apply()
        finally:
            changes.close()
        assert all(item.status == "applied" for item in applied), [
            (item.status, item.error) for item in applied
        ]
    # 不同子目录的同名文件不会互相占用（备份名保留相对路径）；
    # 每轮各产生一个备份：a.txt.bak / a.txt.bak.001 … 与 sub__a.txt.bak / …001
    names = sorted(item.name for item in (tmp_path / "bk" / "retry").iterdir())
    assert names == sorted(
        ["a.txt.bak"] + [f"a.txt.bak.{n:03d}" for n in range(1, 5)]
        + ["sub__a.txt.bak"] + [f"sub__a.txt.bak.{n:03d}" for n in range(1, 5)]
    )
    assert target.read_text() == "v4" and nested_target.read_text() == "s4"


async def test_broker_creates_missing_backup_root(tmp_path) -> None:
    """配置的备份根不存在时，broker 必须自己补目录而不是让所有写入失败。"""
    root = fixture_workspace(tmp_path)
    target = root / "a.txt"
    target.write_text("v1")
    missing = tmp_path / "not-yet" / "backups"
    harness = ConsoleHarness(
        workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")}
    )
    try:
        broker = make_broker(harness, root, "edit", backup_root=missing)
        staged = await broker.execute({
            "action": "propose_changes", "changes": [{"path": "a.txt", "content": "v2"}],
        })
        assert staged.ok, staged.error
        applied = broker.commit()
        assert applied[0].status == "applied", applied[0]
        assert Path(applied[0].backup).read_text() == "v1"
        assert missing.is_dir()
    finally:
        await harness.aclose()


# ---------- R05 父链替换竞态 ----------


async def test_read_cannot_follow_swapped_parent(tmp_path) -> None:
    root = fixture_workspace(tmp_path)
    child = root / "child"
    child.mkdir()
    (child / "memo.txt").write_text("allowed")
    outside = real(tmp_path) / "outside"
    outside.mkdir()
    (outside / "memo.txt").write_text("SYNTHETIC_PARENT_RACE_MARKER")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root)
        original_open = broker._open_regular

        def swap_parent(path, **kwargs):
            child.rename(root / "saved-child")
            child.symlink_to(outside, target_is_directory=True)
            return original_open(path, **kwargs)

        with patch.object(broker, "_open_regular", swap_parent):
            result = await broker.execute({"action": "read", "path": "child/memo.txt"})
        assert "SYNTHETIC_PARENT_RACE_MARKER" not in str(result.payload), result.payload
    finally:
        await harness.aclose()


async def test_search_cannot_follow_swapped_parent(tmp_path) -> None:
    root = fixture_workspace(tmp_path)
    child = root / "child"
    child.mkdir()
    (child / "memo.txt").write_text("allowed")
    outside = real(tmp_path) / "outside"
    outside.mkdir()
    (outside / "memo.txt").write_text("SYNTHETIC_SEARCH_RACE_MARKER")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root)

        def swap_parent(*_args, **_kwargs):
            if child.exists() and not child.is_symlink():
                child.rename(root / "saved-child")
                child.symlink_to(outside, target_is_directory=True)
            return None

        with patch.object(broker, "_open_regular", swap_parent):
            result = await broker.execute({"action": "search", "query": "SYNTHETIC_SEARCH_RACE_MARKER"})
        assert "SYNTHETIC_SEARCH_RACE_MARKER" not in json.dumps(result.payload, ensure_ascii=False)
    finally:
        await harness.aclose()


def test_safe_root_rejects_symlinked_component(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "memo.txt").write_text("secret")
    (root / "link").symlink_to(outside, target_is_directory=True)
    safe = SafeRoot(root)
    try:
        with pytest.raises(SymlinkRejected):
            safe.open_regular(("link", "memo.txt"))
        with pytest.raises(SymlinkRejected):
            safe.mkdir(("link", "newdir"))
        with pytest.raises(SymlinkRejected):
            safe.scandir(("link",))
    finally:
        safe.close()
    assert not (outside / "newdir").exists()


async def test_commit_cannot_be_redirected_by_swapped_parent(tmp_path) -> None:
    """提交阶段的原子替换同样不能被父目录替换引到工作区之外。"""
    root = fixture_workspace(tmp_path)
    sub = root / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("v1")
    outside = real(tmp_path) / "outside"
    outside.mkdir()
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root, "edit")
        staged = await broker.execute({
            "action": "propose_changes",
            "changes": [{"path": "sub/a.txt", "content": "v2"}],
        })
        assert staged.ok, staged.error
        sub.rename(root / "saved-sub")
        sub.symlink_to(outside, target_is_directory=True)
        applied = broker.commit()
        assert not (outside / "a.txt").exists(), "commit followed a swapped parent directory"
        assert applied[0].status == "failed", applied[0]
    finally:
        await harness.aclose()


# ---------- R06 内容摘要 vs 文件版本 ----------


async def test_partial_read_does_not_create_false_conflict(tmp_path) -> None:
    root = fixture_workspace(tmp_path)
    (root / "a.txt").write_text("abcdef")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root, "edit")
        read = await broker.execute({"action": "read", "path": "a.txt", "max_bytes": 3})
        assert read.ok and read.payload["truncated"]
        assert read.payload["sha256"] == file_hash_data(b"abc")
        assert read.payload["version_sha256"] == file_hash(root / "a.txt")
        assert read.payload["version_sha256"] != read.payload["sha256"]
        staged = await broker.execute({
            "action": "propose_changes", "changes": [{"path": "a.txt", "content": "updated"}],
        })
        assert staged.ok, staged.error
        assert broker.commit()[0].status == "applied"
        assert (root / "a.txt").read_text() == "updated"
    finally:
        await harness.aclose()


async def test_partial_read_still_detects_real_change(tmp_path) -> None:
    """部分读取之后文件真的被改动，仍然必须拒绝覆盖（不能放弃冲突保护）。"""
    root = fixture_workspace(tmp_path)
    target = root / "a.txt"
    target.write_text("abcdef")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root, "edit")
        read = await broker.execute({"action": "read", "path": "a.txt", "max_bytes": 3})
        assert read.ok and read.payload["version_sha256"]
        target.write_text("abcdef-external")
        refused = await broker.execute({
            "action": "propose_changes", "changes": [{"path": "a.txt", "content": "updated"}],
        })
        assert not refused.ok and ("漂移" in refused.error or "哈希" in refused.error), refused
        assert target.read_text() == "abcdef-external"
    finally:
        await harness.aclose()


async def test_unchanged_file_can_be_rewritten_after_partial_read(tmp_path) -> None:
    """未变化时重复提议必须成功（不误报外部修改）。"""
    root = fixture_workspace(tmp_path)
    (root / "a.txt").write_text("0123456789")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(harness, root, "edit")
        for _ in range(3):
            read = await broker.execute({"action": "read", "path": "a.txt", "max_bytes": 4})
            assert read.ok
            staged = await broker.execute({
                "action": "propose_changes", "changes": [{"path": "a.txt", "content": "new"}]})
            assert staged.ok, staged.error
        assert broker.commit()[0].status == "applied"
    finally:
        await harness.aclose()


async def test_search_fingerprint_is_full_file_version(tmp_path) -> None:
    """search 命中后用于漂移判定的必须是全文件版本，不是截断片段摘要。"""
    root = fixture_workspace(tmp_path)
    target = root / "big.txt"
    target.write_text("NEEDLE" + "x" * 5000)
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    try:
        broker = make_broker(
            harness, root, "edit", budget=BrokerBudget(max_file_bytes=64),
        )
        found = await broker.execute({"action": "search", "query": "NEEDLE"})
        assert found.ok and found.payload["hits"], found.payload
        assert broker.input_fingerprints()[str(target)] == file_hash(target)
        staged = await broker.execute({
            "action": "propose_changes", "changes": [{"path": "big.txt", "content": "short"}]})
        assert staged.ok, staged.error
    finally:
        await harness.aclose()


async def test_read_is_refused_when_version_cannot_be_established(tmp_path) -> None:
    """超出可安全读取上限时明确失败，不返回"像版本一样"的片段摘要。"""
    from app.security import safeopen

    root = fixture_workspace(tmp_path)
    (root / "big.txt").write_text("y" * 8192)
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"deepseek": FakeAgent("deepseek")})
    original = safeopen.version_read_limit()
    safeopen.set_version_read_limit(1024)
    try:
        broker = make_broker(harness, root, "read")
        result = await broker.execute({"action": "read", "path": "big.txt"})
        assert not result.ok and "上限" in result.error, result
    finally:
        safeopen.set_version_read_limit(original)
        await harness.aclose()


def file_hash_data(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
