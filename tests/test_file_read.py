"""受限读取测试：`/task <agent> --dir ... --mode read` 与公共 broker 的只读动作。

新契约（见 app/commands/registry.py、app/workspaces/broker.py）：
- 读取不再是独立口令 `/read`；它是统一任务入口的一种模式：
  `/task deepseek --dir "<授权目录>" --mode read -- <问题>`；
- 模型只能返回结构化 JSON 动作（list/read/search），服务端逐动作实时校验；
- 越界（绝对路径、`..`、符号链接逃逸、授权根之外、敏感文件）一律明确失败，
  绝不读取；read 模式不接受写动作，这是硬约束而非"提示模型不要写"。

需要读取的目录必须显式授权：`workspace_settings(tmp_path, root=...)` 或 `/workspace use`。
"""

from __future__ import annotations

import json

import pytest

from app.security.acl import AclService, RootGrant
from app.security.authz import Authorizer
from app.workspaces.broker import FileBroker
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    OTHER_USER,
    ScriptedAgent,
    workspace_settings,
)

MODES = frozenset({"read", "edit", "exec"})


def make_broker(root, *, mode: str = "read", user_id: int = ALLOWED_USER) -> FileBroker:
    """一个只授权 root 的 broker（ACL 与线上同一实现）。"""
    authz = Authorizer([ALLOWED_USER], [ALLOWED_GROUP])
    acl = AclService(
        authz=authz,
        grants=(RootGrant(path=root.resolve(), modes=MODES, admin_modes=MODES),),
    )
    return FileBroker(
        acl=acl, root=root.resolve(), group_id=ALLOWED_GROUP, user_id=user_id,
        task_id="t1", mode=mode,
    )


# ---------- broker 读取动作本身 ----------

async def test_broker_reads_text_inside_root(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("你好\n世界", encoding="utf-8")
    result = await make_broker(tmp_path).execute({"action": "read", "path": "notes/a.md"})
    assert result.ok is True and result.fatal is False
    assert result.payload["text"] == "你好\n世界"
    assert result.payload["path"] == "notes/a.md"
    assert result.payload["truncated"] is False


@pytest.mark.parametrize("evil", [
    "../outside.txt",
    "notes/../../outside.txt",
    "/etc/passwd",
    "~/secret.txt",
])
async def test_broker_rejects_path_escape(tmp_path, evil):
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    result = await make_broker(tmp_path).execute({"action": "read", "path": evil})
    assert result.ok is False
    # 越界是权限级失败：任务必须明确失败，不能继续循环或降级
    assert result.fatal is True
    assert "text" not in result.payload


async def test_broker_rejects_symlink_escape(tmp_path):
    """符号链接指向根目录外时同样拒绝（resolve 之后再判定边界，不做字符串前缀比较）。"""
    outside = tmp_path.parent / "linked.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    result = await make_broker(tmp_path).execute({"action": "read", "path": "link.txt"})
    assert result.ok is False and result.fatal is True
    assert "超出" in result.error and "secret" not in json.dumps(result.payload)


async def test_broker_rejects_binary_directory_and_missing(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "adir").mkdir()
    broker = make_broker(tmp_path)
    for path, expected in (("bin.dat", "二进制"), ("adir", "目录"), ("missing.txt", "不存在")):
        result = await broker.execute({"action": "read", "path": path})
        assert result.ok is False and result.fatal is False, "可恢复错误应回灌给模型"
        assert expected in result.error


async def test_broker_truncates_at_limit(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 5000, encoding="utf-8")
    result = await make_broker(tmp_path).execute(
        {"action": "read", "path": "big.txt", "max_bytes": 1000}
    )
    assert result.ok is True
    assert result.payload["bytes"] == 1000
    assert len(result.payload["text"]) == 1000
    assert result.payload["truncated"] is True


async def test_broker_list_and_search_skip_sensitive_files(tmp_path):
    (tmp_path / "a.txt").write_text("正文：先抓日志再限流", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    broker = make_broker(tmp_path)

    listed = await broker.execute({"action": "list", "path": "."})
    names = {entry["path"] for entry in listed.payload["entries"]}
    assert "a.txt" in names and ".env" not in names

    found = await broker.execute({"action": "search", "query": "先抓日志"})
    assert [hit["path"] for hit in found.payload["hits"]] == ["a.txt"]
    assert found.payload["hits"][0]["line"] == 1


async def test_broker_read_mode_refuses_write_actions(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("原始内容", encoding="utf-8")
    result = await make_broker(tmp_path, mode="read").execute(
        {"action": "propose_changes", "changes": [{"path": "a.txt", "content": "改写"}]}
    )
    assert result.ok is False and result.fatal is True
    assert "read" in result.error and "edit" in result.error
    assert target.read_text(encoding="utf-8") == "原始内容"


async def test_broker_refuses_write_for_unauthorized_user(tmp_path):
    """撤权/未授权用户在 broker 层被实时拒绝（不依赖调用方先前的判定）。"""
    (tmp_path / "a.txt").write_text("原始内容", encoding="utf-8")
    broker = make_broker(tmp_path, mode="edit", user_id=OTHER_USER)
    result = await broker.execute(
        {"action": "propose_changes", "changes": [{"path": "a.txt", "content": "改写"}]}
    )
    assert result.ok is False
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "原始内容"


# ---------- 命令层 ----------

async def test_read_task_feeds_file_content_to_agent(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "plan.md").write_text("方案：先抓日志再限流", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    deepseek = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "plan.md"}]}),
        json.dumps({"final": "已读：风险是先限流后抓日志会丢信息"}),
    ], display_name="DeepSeek")
    harness = ConsoleHarness(settings, {"deepseek": deepseek})

    out = await harness.command(
        f'/task deepseek --dir "{root}" --mode read -- 这个方案有什么风险？'
    )

    # 问题正文进入 broker 协议消息，文件内容作为动作结果回灌给模型
    first_call = "\n".join(message.content for message in deepseek.calls[0])
    assert "这个方案有什么风险？" in first_call
    action_result = "\n".join(message.content for message in deepseek.calls[1])
    assert "先抓日志再限流" in action_result
    assert "plan.md" in action_result
    # 任务成功，且模型是通过 broker 读到的文件（有操作记录）
    assert "模式：read" in out and "已读" in out
    assert "broker:read plan.md" in out
    assert "失败" not in out


async def test_read_task_never_modifies_directory(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("原始内容", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    deepseek = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "只读完成"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": deepseek})

    await harness.command(f'/task deepseek --dir "{root}" --mode read -- 改写成别的')

    assert target.read_text(encoding="utf-8") == "原始内容"
    assert sorted(path.name for path in root.iterdir()) == ["a.txt"]


async def test_read_mode_write_attempt_fails_without_side_effects(tmp_path):
    """read 模式下模型提出修改：任务明确失败，文件与备份都不产生。"""
    root = tmp_path / "files"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("原始内容", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [
            {"action": "propose_changes", "changes": [{"path": "a.txt", "content": "改写"}]}
        ]}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 改文件')

    assert "失败" in out and "不支持" not in out
    assert "不允许提出文件修改" in out
    assert target.read_text(encoding="utf-8") == "原始内容"
    assert sorted(path.name for path in root.iterdir()) == ["a.txt"]


async def test_model_escape_attempt_does_not_leak_secret(tmp_path):
    """模型尝试读授权根之外的文件：任务失败，内容不进入回复。"""
    root = tmp_path / "files"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "../secret.txt"}]}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 读取')

    assert "失败" in out and "不允许包含 .." in out
    assert "TOP-SECRET-CONTENT" not in out
    assert len(agent.calls) == 1, "越界动作是权限级失败，不得继续循环"


async def test_out_of_root_and_unauthorized_never_call_agent(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("私密", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="不该被调用")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    # 未授权用户
    out = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 读', user_id=OTHER_USER)
    assert "未授权" in out
    # 授权根之外（/etc 不在任何 workspaces.roots 内）
    out = await harness.command('/task deepseek --dir "/etc" --mode read -- 读')
    assert "不在授权根内" in out
    # 给目录之外：不存在
    out = await harness.command(f'/task deepseek --dir "{root}/missing" --mode read -- 读')
    assert "目录不存在" in out
    # 文件模式缺目录，且绝不回落到进程工作目录
    out = await harness.command("/task deepseek --mode read -- 读")
    assert "需要目录" in out and "不会回落到进程工作目录" in out

    assert agent.calls == []


async def test_symlink_directory_escape_rejected_before_model_call(tmp_path):
    """授权根内的符号链接指向根外目录：规范化后按真实路径判定，仍被拒绝。"""
    root = tmp_path / "files"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("私密", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="不该被调用")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}/escape" --mode read -- 读')

    assert "不在授权根内" in out
    assert agent.calls == []


async def test_read_requires_authorized_root_configured(tmp_path):
    """没有配置任何授权根时，任何目录都不可读。"""
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("私密", encoding="utf-8")
    from helpers import make_settings

    agent = FakeAgent("deepseek")
    harness = ConsoleHarness(make_settings(tmp_path), {"deepseek": agent})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 读')

    assert "不在授权根内" in out and agent.calls == []


async def test_file_content_is_untrusted_data_and_outbound_cq_is_neutralized(tmp_path):
    """文件内容是不可信数据：作为动作结果交给模型，回显到群里时 CQ 码必须被打断。"""
    root = tmp_path / "files"
    root.mkdir()
    (root / "evil.txt").write_text("正常内容 [CQ:at,qq=all] 结束", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    deepseek = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "evil.txt"}]}),
        json.dumps({"final": "文件里有 [CQ:at,qq=all] 这样的内容"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": deepseek})

    out = await harness.command(f'/task deepseek --dir "{root}" --mode read -- 总结')

    # 模型拿到的是动作结果（数据），不是被当成指令
    action_result = "\n".join(message.content for message in deepseek.calls[1])
    assert "正常内容" in action_result and "[CQ:at,qq=all]" in action_result
    # 出站消息里 CQ 码被零宽空格打断，不会被 OneBot 解析成富媒体
    assert "[CQ:at" not in out
    assert "文件里有" in out


async def test_container_lesson_full_read_flow_reports_usage_errors(tmp_path):
    """用法错误（未知 Agent / 空正文）在调用前被拒。"""
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    await harness.command(f'/task 不存在的 --dir "{root}" --mode read -- 读')
    assert "未知 Agent" in harness.transport.texts()[-1]
    await harness.command(f'/task deepseek --dir "{root}" --mode read --')
    assert "正文为空" in harness.transport.texts()[-1]
    await harness.command(f'/task deepseek --dir "{root}" --mode read')
    assert "缺少正文分隔符" in harness.transport.texts()[-1]
    assert agent.calls == []


# ---------- 绑定目录（--dir .） ----------

async def test_workspace_binding_then_dir_dot(tmp_path):
    """`/workspace use` 绑定后，`--dir .` 用的是「本群 × 你」的绑定目录。"""
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("绑定内容", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "读到绑定内容"}),
    ])
    harness = ConsoleHarness(settings, {"deepseek": agent})

    receipt = await harness.command(f'/workspace use "{root}"')
    assert "已绑定目录" in receipt
    assert "read" in receipt  # 回执说明该目录上的实际权限

    out = await harness.command("/task deepseek --dir . --mode read -- 读 a.txt")

    assert "读到绑定内容" in out
    assert "绑定内容" in "\n".join(message.content for message in agent.calls[1])
    assert "当前绑定" in await harness.command("/workspace status")
    assert "已清除" in await harness.command("/workspace clear")


async def test_dir_dot_without_binding_never_falls_back_to_cwd(tmp_path):
    """没有绑定时 `--dir .` 明确拒绝，绝不回落到进程工作目录。"""
    root = tmp_path / "files"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="不该被调用")
    harness = ConsoleHarness(settings, {"deepseek": agent})

    out = await harness.command("/task deepseek --dir . --mode read -- 读")

    assert "需要先绑定目录" in out and "不会回落到进程工作目录" in out
    assert agent.calls == []
