"""按群授权、持久化及成员权限边界的行为测试。

新契约（见 app/commands/registry.py、app/routing/dispatcher.py、app/security/acl.py）：
- `/whitelist add|remove|list` → `/access add|remove|list`，仅机器人管理员可用；
- 成员默认只能纯文本；目录 read/edit/exec 来自配置的目录 ACL，
  与聊天里的任何选项、保存的偏好都无关；
- 旧口令（/whitelist、/read、/write、/approve）只回迁移提示，不做任何事。
"""

from __future__ import annotations

import pytest

from helpers import ConsoleHarness, FakeAgent, make_settings


async def test_grant_persists_and_revoke_blocks_after_restart(tmp_path):
    settings = make_settings(tmp_path)
    agent = FakeAgent("deepseek")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    # 未授权：不调用模型，明确回绝
    await harness.command("/task deepseek -- hello", user_id=300)
    assert not agent.calls
    assert "未授权" in harness.transport.texts()[-1]

    await harness.command("/access add 300")
    await harness.command("/task deepseek -- hello", user_id=300)
    assert len(agent.calls) == 1

    # 重启后授权仍在（落库，不依赖内存）
    harness.context_store.close()
    harness2 = ConsoleHarness(settings, {"deepseek": agent})
    await harness2.command("/task deepseek -- after restart", user_id=300)
    assert len(agent.calls) == 2

    await harness2.command("/access remove 300")
    harness2.context_store.close()

    harness3 = ConsoleHarness(settings, {"deepseek": agent})
    await harness3.command("/task deepseek -- revoked", user_id=300)
    assert len(agent.calls) == 2
    assert "未授权" in harness3.transport.texts()[-1]
    harness3.context_store.close()


async def test_grant_does_not_cross_groups(tmp_path):
    harness = ConsoleHarness(
        make_settings(tmp_path, qq_allowed_group_ids=[200, 201]),
        {"deepseek": FakeAgent("deepseek")},
    )
    await harness.command("/access add 300")
    assert harness.authz.user_allowed(300, 200)
    assert not harness.authz.user_allowed(300, 201)

    # 未授权群里的命令被静默忽略，不会产生跨群授权
    await harness.command("/access add 301", group_id=777)
    assert not harness.authz.user_allowed(301, 200)
    assert not harness.authz.user_allowed(300, 777)


@pytest.mark.parametrize("command", ["/access add 400", "/access remove 100", "/access list"])
async def test_guest_cannot_manage_access(tmp_path, command):
    """普通成员（即使是管理员刚授权的）不能管理名单。"""
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": FakeAgent("codex")})
    await harness.command("/access add 300")

    await harness.command(command, user_id=300)

    assert "仅允许机器人管理员" in harness.transport.texts()[-1]
    assert not harness.authz.user_allowed(400, 200)
    assert harness.authz.user_allowed(100, 200)


@pytest.mark.parametrize("command", [
    "/whitelist add 400", "/whitelist remove 100", "/whitelist list",
    "/read codex a", "/write codex a content", "/approve any",
])
async def test_legacy_commands_from_guest_only_migrate(tmp_path, command):
    """旧口令对任何人都只回迁移提示：既不提权，也不产生副作用。"""
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": FakeAgent("codex")})
    await harness.command("/access add 300")

    await harness.command(command, user_id=300)

    assert "旧口令" in harness.transport.texts()[-1]
    assert not harness.authz.user_allowed(400, 200)
    assert harness.authz.user_allowed(100, 200)


async def test_member_directory_access_comes_from_acl_not_options(tmp_path):
    """成员带 --dir 但没有目录 ACL：调用前就被拒；纯文本仍可用。"""
    outside = tmp_path / "not-authorized"
    outside.mkdir()
    settings = make_settings(tmp_path)  # 没有配置 workspaces.roots
    agents = {name: FakeAgent(name) for name in ("codex", "zcode", "deepseek")}
    harness = ConsoleHarness(settings, agents)
    await harness.command("/access add 300")

    await harness.command(
        f'/task codex --dir "{outside}" --mode read -- change a', user_id=300
    )
    assert not agents["codex"].calls
    assert "不在授权根内" in harness.transport.texts()[-1]

    # 成员默认只能文本：/task 与 /task all 都不会带上目录
    await harness.command("/task zcode -- hello", user_id=300)
    await harness.command("/task all -- hello", user_id=300)
    assert all(
        option.mode == "text" and option.directory is None
        for agent in agents.values() for option in agent.options_log
    )

    # 保存模型偏好只影响模型选择，不会把目录/模式一起保存
    await harness.command("/agent set zcode --model fake-zcode", user_id=300)
    assert harness.context_store.preferences(
        group_id=200, user_id=300, agent="zcode"
    ).model == "fake-zcode"
    await harness.command("/task zcode -- saved", user_id=300)
    assert agents["zcode"].options_log[-1].mode == "text"
    assert agents["zcode"].options_log[-1].directory is None


async def test_acl_granted_member_gets_declared_mode(tmp_path):
    """目录权限来自 ACL：授权根内的成员显式 --mode read 才有目录能力。"""
    from helpers import workspace_settings

    root = tmp_path / "authorized"
    root.mkdir()
    (root / "a.txt").write_text("内容", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("zcode")
    harness = ConsoleHarness(settings, {"zcode": agent})
    await harness.command("/access add 300")

    await harness.command(f'/task zcode --dir "{root}" --mode read -- 读', user_id=300)

    # 成员在授权根内、显式声明 read：走公共 broker，且模式如实记录为 read
    assert len(agent.calls) >= 1
    options = agent.options_log[0]
    assert options.mode == "read"
    assert options.directory == str(root.resolve())
    assert agent.calls[0][0].role == "system"
    assert "broker" in agent.calls[0][0].content


@pytest.mark.parametrize("target", [
    "0", "-1", "abc", "9223372036854775808", "300 extra", "100", "999", "400",
])
async def test_invalid_or_protected_targets_do_not_gain_access(tmp_path, target):
    harness = ConsoleHarness(make_settings(tmp_path, qq_ignore_user_ids=[400]), {})
    await harness.command("/access add " + target)
    assert harness.context_store.whitelist(200) == {}
    await harness.command("/access remove 100")
    assert harness.authz.admin_allowed(100)
    assert harness.authz.user_allowed(100, 200)


async def test_list_reflects_grants_and_revocation(tmp_path):
    harness = ConsoleHarness(make_settings(tmp_path), {})
    await harness.command("/access add 300")
    await harness.command("/access list")
    assert "100（管理员）" in harness.transport.texts()[-1]
    assert "300" in harness.transport.texts()[-1]

    await harness.command("/access remove 300")
    await harness.command("/access list")
    assert "300" not in harness.transport.texts()[-1]


async def test_access_usage_errors(tmp_path):
    harness = ConsoleHarness(make_settings(tmp_path), {})
    await harness.command("/access")
    assert "用法" in harness.transport.texts()[-1]
    await harness.command("/access add")
    assert "用法" in harness.transport.texts()[-1]
