"""A01/A02/A03：新口令唯一性、统一公共语法、注册表与 preset 扩展。

对应验收标准：
- A01 新口令唯一性：help/discovery 与实际路由一致；旧口令只提示迁移，
  模型调用次数、文件哈希、后台任务均不变；
- A02 统一公共语法：中文/空格路径、引号、换行、正文 `--`、
  重复/未知/缺值与 dir/find 冲突全部按规格处理；
- A03 扩展口令：新增一个 preset 不需要改解析器/Dispatcher/team，帮助自动出现，
  且 preset 不能提升权限。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.commands.options import OptionError, parse_task_arguments
from app.commands.presets import PresetError, PresetSpec, expand_preset
from app.commands.registry import CommandRegistry, LEGACY_COMMANDS
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    make_message,
    workspace_settings,
)

EXPECTED_COMMANDS = {"task", "workspace", "agent", "team", "confirm", "access", "command", "help"}
LEGACY = sorted(LEGACY_COMMANDS)


def test_registry_contains_only_canonical_commands() -> None:
    registry = CommandRegistry()
    assert set(registry.names()) == EXPECTED_COMMANDS
    for name in LEGACY:
        assert registry.get(name) is None, f"{name} 不应被注册为命令"


def test_discovery_matches_help_and_routing() -> None:
    registry = CommandRegistry()
    discovery = {item["name"] for item in registry.discovery()}
    assert discovery == EXPECTED_COMMANDS
    help_text = registry.help_text()
    usage_lines = [line for line in help_text.splitlines() if line.startswith("/")]
    for name in EXPECTED_COMMANDS:
        assert any(line.startswith(f"/{name}") for line in usage_lines), name
    for name in LEGACY:
        assert not any(line.startswith(f"/{name} ") for line in usage_lines), name


def test_reserved_names_detected() -> None:
    registry = CommandRegistry()
    for reserved in ("all", "status", "result", "cancel"):
        assert reserved in registry.reserved_names()
        # 保留名不能被注册为命令
        with pytest.raises(ValueError):
            registry.register(
                __import__("app.commands.spec", fromlist=["CommandSpec"]).CommandSpec(
                    name=reserved, description="x", usage="/x", handler_key="x"
                )
            )


def test_handler_must_be_implemented() -> None:
    registry = CommandRegistry()
    with pytest.raises(ValueError):
        registry.verify()
    registry.bind_handlers({"task", "workspace", "agent", "team", "confirm", "access", "command", "help"})
    registry.verify()


async def test_legacy_commands_only_migrate_and_change_nothing(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("原始内容\n", encoding="utf-8")
    before = target.read_bytes()
    settings = workspace_settings(tmp_path, root=root)
    agent = FakeAgent("deepseek", reply="不应被调用")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        for text in (
            "/deepseek 你好",
            "/read deepseek note.txt",
            "/write deepseek note.txt 新内容",
            "/approve ABCD1234",
            "/whitelist add 400",
            "/models deepseek",
            "/set deepseek --model fake-deepseek",
            "/status",
            "/clear deepseek",
            "/all 大家好",
            "/glm 你好",
            "/gpt 你好",
            "/zcode 你好",
            "/codex --dir /tmp 你好",
        ):
            reply = await harness.command(text)
            assert "旧口令" in reply, text
            assert "不会" in reply
        # 模型没有被调用、文件没有被修改、没有后台任务
        assert agent.calls == []
        assert target.read_bytes() == before
        assert harness.task_store.list_recent(group_id=ALLOWED_GROUP, user_id=ALLOWED_USER) == []
        assert harness.runner.active_teams == ()
    finally:
        await harness.aclose()


async def test_unknown_command_is_silent(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        before = harness.send_count()
        await harness.command("/definitely-not-a-command hello")
        assert harness.send_count() == before
    finally:
        await harness.aclose()


# ---------- A02 统一公共语法 ----------


def _parse(header: str):
    registry = CommandRegistry()
    return parse_task_arguments(header, spec=registry.get("task"))


def test_option_parser_supports_chinese_and_spaces() -> None:
    parsed = _parse('--dir "/Users/example/Documents/我的 项目" --mode edit -- 修改 README')
    assert parsed.directory == "/Users/example/Documents/我的 项目"
    assert parsed.mode == "edit"
    assert parsed.body == "修改 README"


def test_option_parser_preserves_body_verbatim() -> None:
    body = '第一行\n--mode exec\n第二行 "引号" 和 空行\n\n结束'
    parsed = _parse(f"--mode read --dir /tmp -- {body}")
    assert parsed.body == body


def test_option_parser_equals_form_and_default_effort() -> None:
    parsed = _parse("--model=glm-5 --effort=default -- 问题")
    assert parsed.model == "glm-5"
    assert parsed.effort == "default"
    assert parsed.effort_set is True


@pytest.mark.parametrize(
    "header",
    [
        "--dir /a --find b -- x",              # 冲突
        "--mode text --dir /a -- x",           # 文本模式不接受目录
        "--mode bogus -- x",                   # 非法枚举
        "--unknown 1 -- x",                    # 未知选项
        "--dir /a --dir /b -- x",              # 重复选项
        "--model --dir /a -- x",               # 缺值
        '--dir "/tmp/未闭合 -- x',              # 引号未闭合
        "no-options-here -- x",                # 选项区出现无法识别的内容
    ],
)
def test_option_parser_rejects_invalid_headers(header: str) -> None:
    with pytest.raises(OptionError):
        _parse(header)


def test_mode_defaults_follow_spec() -> None:
    assert _parse("-- 纯文本").resolved_mode() == "text"
    assert _parse("--dir /tmp -- 读").resolved_mode() == "read"
    assert _parse("--find 项目 -- 读").resolved_mode() == "read"
    assert _parse("--mode exec --dir /tmp -- 跑").resolved_mode() == "exec"


async def test_task_all_rejects_capability_options(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        reply = await harness.command("/task all --mode read --dir /tmp -- 一起看看")
        assert "不接受" in reply
    finally:
        await harness.aclose()


async def test_task_all_runs_each_agent_as_text(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    agents = {
        "deepseek": FakeAgent("deepseek", reply="A"),
        "zcode": FakeAgent("zcode", reply="B"),
    }
    harness = ConsoleHarness(settings, agents)
    try:
        reply = await harness.command("/task all -- 请分别回答")
        assert "A" in reply and "B" in reply
        for agent in agents.values():
            assert agent.options_log[-1].mode == "text"
            assert agent.options_log[-1].directory is None
    finally:
        await harness.aclose()


# ---------- A03 preset 扩展 ----------


def test_preset_expands_without_touching_core() -> None:
    preset = PresetSpec(
        name="review", description="评审", agent="deepseek", mode="read",
        template="请评审以下改动并关注兼容性：{task}",
    )
    options = _parse('--dir "/tmp/项目" -- 关注兼容性')
    expanded = expand_preset(preset, options)
    assert expanded.agent == "deepseek"
    assert expanded.mode == "read"
    assert expanded.body.endswith("关注兼容性")
    assert expanded.directory == "/tmp/项目"


def test_preset_cannot_escalate() -> None:
    preset = PresetSpec(
        name="ask", description="纯文本提问", agent="deepseek", mode="text",
        template="{task}", allow_overrides=(),
    )
    options = _parse("--mode exec -- 提权")
    with pytest.raises(PresetError):
        expand_preset(preset, options)


async def test_preset_registered_in_harness_appears_in_help_and_runs(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    agent = FakeAgent("deepseek", reply="评审完成")
    harness = ConsoleHarness(
        settings, {"deepseek": agent},
        presets={
            "review": PresetSpec(
                name="review", description="代码评审", agent="deepseek", mode="text",
                template="评审：{task}",
            )
        },
    )
    try:
        listing = await harness.command("/command list")
        assert "/review" in listing
        shown = await harness.command("/command show review")
        assert "preset" in shown
        reply = await harness.command("/review -- 关注兼容性")
        assert "评审完成" in reply
        # 模板做了文本替换，正文被嵌进模板
        assert "评审：关注兼容性" in agent.last_prompt()
    finally:
        await harness.aclose()


async def test_preset_conflicting_with_command_is_rejected() -> None:
    from app.commands.registry import CommandRegistry

    registry = CommandRegistry()
    with pytest.raises(ValueError):
        registry.register_presets({"task": PresetSpec(
            name="task", description="x", agent="deepseek", template="{task}"
        )})
    with pytest.raises(ValueError):
        registry.register_presets({"status": PresetSpec(
            name="status", description="x", agent="deepseek", template="{task}"
        )})


def test_preset_config_parsing_reports_bad_field() -> None:
    from app.commands.presets import PresetError as Error
    from app.commands.presets import presets_from_config

    with pytest.raises(Error):
        presets_from_config({"x": {"agent": "a", "template": "{task}", "bogus": 1}})
    parsed = presets_from_config(
        {"summarize": {"agent": "deepseek", "template": "总结：{task}", "mode": "text"}}
    )
    assert parsed["summarize"].agent == "deepseek"


async def test_help_family_and_command_show(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        family = await harness.command("/help task")
        assert "--mode" in family and "--dir" in family
        shown = await harness.command("/command show workspace")
        assert "/workspace find" in shown
        unknown = await harness.command("/command show nope")
        assert "未知命令族" in unknown
    finally:
        await harness.aclose()


def test_dispatcher_contract_has_no_agent_name_branches() -> None:
    """目录能力不得再由 agent 名字决定（源码级约束）。"""
    source = Path("app/routing/dispatcher.py").read_text(encoding="utf-8")
    for needle in ('name == "codex"', "name == 'codex'", '== "zcode"'):
        assert needle not in source
    base = Path("app/agents/base.py").read_text(encoding="utf-8")
    assert 'self.name != "codex"' not in base


def test_asyncio_helpers_are_used_consistently() -> None:
    # 保证测试文件自身的 asyncio 使用正确（防止误用同步上下文）
    assert asyncio.iscoroutinefunction(ConsoleHarness.command)


def test_broker_json_protocol_visible_in_system_prompt(tmp_path) -> None:
    from app.backends.broker_loop import build_broker_messages

    messages = build_broker_messages(prompt="做点什么", workspace="/tmp", mode="read")
    payload = json.loads(messages[-1].content)
    assert payload["mode"] == "read"
    assert "propose_changes" in messages[0].content


async def test_agent_set_persists_backend_model_and_effort(tmp_path) -> None:
    """/agent set 保存的后端必须真的持久化，并优先于 Agent 默认后端。"""
    settings = workspace_settings(tmp_path)
    agent = FakeAgent(
        "deepseek",
        model_options={"fake-deepseek": (), "alternate": ("low", "high")},
    )
    other = FakeAgent("zcode", reply="另一个后端")
    harness = ConsoleHarness(
        settings, {"deepseek": agent, "zcode": other},
        from_settings=False,
    )
    # 构造第二个后端挂在同一 Agent 上，验证 --backend 选择生效
    harness.backends._add_backend(  # noqa: SLF001 - 测试直接登记第二个后端
        __import__("app.backends.factory", fromlist=["BackendConfig"]).BackendConfig(
            id="deepseek_backup", adapter="fake", agent="deepseek",
        ),
        other, backend_id="deepseek_backup", agent="deepseek", exec_policy_enabled=True,
    )
    harness.backends.agents["deepseek"].allowed_backends = ("deepseek", "deepseek_backup")
    try:
        saved = await harness.command(
            "/agent set deepseek --backend deepseek_backup --model fake-zcode --effort default"
        )
        assert "已保存默认" in saved
        stored = harness.context_store.preferences(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="deepseek"
        )
        assert stored.backend == "deepseek_backup"
        reply = await harness.command("/task deepseek -- 用保存的后端")
        assert "另一个后端" in reply
        assert other.calls and not agent.calls
        # 显式 --backend 仍然可以覆盖保存值
        reply2 = await harness.command("/task deepseek --backend deepseek --model fake-deepseek -- 换回来")
        assert "fake-reply" in reply2
    finally:
        await harness.aclose()


async def _wait_for_task_record(harness, user_id: int, timeout: float = 2.0):
    """等待成员的任务记录出现（避免用固定 sleep 造成的时序抖动）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        records = harness.task_store.list_recent(
            group_id=ALLOWED_GROUP, user_id=user_id, limit=1
        )
        if records:
            return records
        await asyncio.sleep(0.01)
    return []


async def test_access_revocation_cancels_member_running_task(tmp_path) -> None:
    """撤销授权立即生效：成员未完成的任务被取消，晚到结果不覆盖终态。"""
    settings = workspace_settings(tmp_path)  # 100 是管理员
    member = 555
    settings.qq_allowed_user_ids = [ALLOWED_USER]
    agent = FakeAgent("deepseek", reply="慢任务完成", delay=0.5)
    harness = ConsoleHarness(settings, {"deepseek": agent})
    try:
        await harness.command(f"/access add {member}")
        runner = asyncio.create_task(
            harness.dispatcher.handle_message(make_message("/task deepseek -- 慢任务", user_id=member))
        )
        records = await _wait_for_task_record(harness, member)
        assert records and records[0]["status"] in {"queued", "running"}
        await harness.command(f"/access remove {member}")
        # 取消等待/执行中的任务：协程正常收敛（不再向上抛 CancelledError），
        # 但不得调用模型、不得写入文件
        outcome = await runner
        assert outcome is None or outcome.ok is False
        record = harness.task_store.get_task(records[0]["task_id"])
        assert record["status"] == "cancelled"
        # 撤销前已经开始的那一次调用不算越权；关键是撤销后不再产生新的调用与写入
        assert len(agent.calls) == 1
        assert harness.task_store.list_changes(task_id=record["task_id"]) == []
        events = [item[0] for item in harness.task_store.events(records[0]["task_id"])]
        assert "cancelled" in events
    finally:
        await harness.aclose()
