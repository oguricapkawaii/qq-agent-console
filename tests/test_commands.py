"""命令路由测试：统一命令族路由、旧口令只给迁移提示、未授权拒绝、普通聊天忽略。

新契约（见 app/commands/registry.py）：
- 只有 /task、/workspace、/agent、/team、/confirm、/access、/command、/help 是命令；
- 旧口令（/glm、/read、/write、/all、/models、/set、/status、/clear、/approve、
  /whitelist）**不是**兼容层：只回一段迁移提示，不调用模型、不读写文件。
"""

from __future__ import annotations

import re

import pytest

from app.commands.registry import LEGACY_COMMANDS, CommandRegistry
from helpers import (
    BOT_SELF,
    FakeAgent,
    build_dispatcher,
    make_message,
    make_settings,
)

ALL_LEGACY_TEXTS = [
    "/glm 分析这段日志",
    "/deepseek 审查这个方案",
    "/zcode 改一下",
    "/gpt 总结一下",
    "/codex 检查这个项目",
    "/all 这个方案怎么样",
    "/read deepseek a.txt 这个方案有什么风险？",
    "/write deepseek a.txt 新内容",
    "/models deepseek",
    "/set deepseek --model fake",
    "/status",
    "/clear deepseek",
    "/approve DEADBEEF",
    "/whitelist add 300",
]


async def test_task_routes_to_named_agent(tmp_path):
    """/task glm|deepseek|gpt 分别路由到对应 Agent。"""
    settings = make_settings(tmp_path)
    agents = {
        "glm": FakeAgent("glm"),
        "deepseek": FakeAgent("deepseek"),
        "gpt": FakeAgent("gpt"),
    }
    bot, transport = build_dispatcher(settings, agents)

    await bot.handle_message(make_message("/task glm -- 分析这段日志"))
    await bot.handle_message(make_message("/task deepseek -- 审查这个方案"))
    await bot.handle_message(make_message("/task gpt -- 总结一下"))

    assert len(agents["glm"].calls) == 1
    assert len(agents["deepseek"].calls) == 1
    assert len(agents["gpt"].calls) == 1
    # 每个 Agent 收到的最后一条 user 消息就是对应问题正文
    assert agents["glm"].last_prompt() == "分析这段日志"
    assert agents["deepseek"].last_prompt() == "审查这个方案"
    assert agents["gpt"].last_prompt() == "总结一下"
    # 纯文本任务：不携带任何目录/工具能力
    for agent in agents.values():
        assert agent.options_log[-1].mode == "text"
        assert agent.options_log[-1].directory is None
        assert agent.options_log[-1].workspace_tools is False
    # 回复带 Agent 展示名和 task_id
    assert any("[GLM · task " in t for t in transport.texts())
    assert any("[DEEPSEEK · task " in t for t in transport.texts())
    assert any("[GPT · task " in t for t in transport.texts())


async def test_task_body_is_preserved_after_separator(tmp_path):
    """`--` 之后的正文原样保留：空行、以 `--` 开头的内容、多行都不再被解释为选项。"""
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)

    body = "第一行\n\n--effort ultra 不是选项\n第二行"
    await bot.handle_message(make_message(f"/task glm -- {body}"))

    assert agents["glm"].last_prompt() == body
    # 正文里的 "--effort ultra" 没有变成强度选择
    assert agents["glm"].options_log[-1].effort is None
    assert not any("不受模型" in t for t in transport.texts())


@pytest.mark.parametrize("text", ALL_LEGACY_TEXTS)
async def test_legacy_commands_are_migration_hints_only(tmp_path, text):
    """旧口令只返回迁移提示：不调用模型，也不产生任何文件副作用。"""
    settings = make_settings(tmp_path)
    (tmp_path / "a.txt").write_text("原始内容", encoding="utf-8")
    agents = {name: FakeAgent(name) for name in ("glm", "deepseek", "zcode", "gpt", "codex")}
    bot, transport = build_dispatcher(settings, agents)

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    await bot.handle_message(make_message(text))
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    joined = "\n".join(transport.texts())
    assert "旧口令" in joined, f"{text} 应返回迁移提示"
    assert "没有兼容执行层" in joined
    # 旧命令名对应的新写法出现在提示里
    name = text.split()[0].lstrip("/")
    assert LEGACY_COMMANDS[name] in joined
    assert all(agent.calls == [] for agent in agents.values()), "旧口令不得调用模型"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "原始内容"
    assert before == after, "旧口令不得产生任何文件副作用"


async def test_normal_chat_does_not_trigger_agents(bot, agents):
    dispatcher, _ = bot
    await dispatcher.handle_message(make_message("今天天气不错"))
    await dispatcher.handle_message(make_message("请问 /task 是什么意思？"))  # 命令不在开头
    await dispatcher.handle_message(make_message("/foo bar"))
    await dispatcher.handle_message(make_message("/taskx 你好"))
    await dispatcher.handle_message(make_message("/glmx 你好"))
    assert all(agent.calls == [] for agent in agents.values())


async def test_self_message_ignored(bot, agents):
    """机器人自己发出的消息（含命令）必须被忽略。"""
    dispatcher, _ = bot
    await dispatcher.handle_message(
        make_message("/task glm -- 你好", user_id=BOT_SELF, self_id=BOT_SELF)
    )
    assert agents["glm"].calls == []


async def test_ignored_user_id_is_silent(tmp_path):
    settings = make_settings(tmp_path, qq_ignore_user_ids=[555])
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("/task glm -- 你好", user_id=555))
    assert agents["glm"].calls == []
    assert transport.texts() == []


async def test_unauthorized_user_rejected(tmp_path):
    """未授权用户不能调用模型，收到拒绝消息。"""
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("/task glm -- 你好", user_id=42))
    assert agents["glm"].calls == []
    assert any("未授权" in t for t in transport.texts())


async def test_unauthorized_user_can_use_help(tmp_path):
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("/help", user_id=42))
    assert any("可用命令" in t for t in transport.texts())
    # 但需要授权的命令（原 /status 的替代：/agent status）依然被拒
    await bot.handle_message(make_message("/agent status", user_id=42))
    assert any("未授权" in t for t in transport.texts())
    assert agents["glm"].calls == []


async def test_unlisted_group_is_silent(tmp_path):
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("/task glm -- 你好", group_id=888888))
    assert agents["glm"].calls == []
    assert transport.texts() == []


async def test_usage_errors_do_not_call_models(tmp_path):
    """缺正文、缺分隔符、未知 Agent、未知选项都在调用前被拒。"""
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)

    await bot.handle_message(make_message("/task glm"))
    assert any("正文为空" in t for t in transport.texts())

    await bot.handle_message(make_message("/task glm --effort high 你好"))
    assert any("缺少正文分隔符" in t for t in transport.texts())

    await bot.handle_message(make_message("/task 不存在的 -- 你好"))
    assert any("未知 Agent" in t for t in transport.texts())

    await bot.handle_message(make_message("/task glm --unknown x -- 你好"))
    assert any("未知选项" in t for t in transport.texts())

    assert agents["glm"].calls == []


async def test_help_lists_registered_command_families(tmp_path):
    """帮助由注册表生成：列出新命令族，并说明旧口令已停用。"""
    settings = make_settings(tmp_path)
    bot, transport = build_dispatcher(settings, {"glm": FakeAgent("glm")})
    await bot.handle_message(make_message("/help"))
    joined = "\n".join(transport.texts())
    for cmd in ("/task", "/workspace", "/agent", "/team", "/confirm", "/access", "/command", "/help"):
        assert cmd in joined
    assert "旧口令" in joined and "已停用" in joined


async def test_agent_status_reports_configured_and_unconfigured(tmp_path):
    """/status 的替代：/agent status 如实展示已配置/未配置。"""
    settings = make_settings(tmp_path)
    agents = {
        "glm": FakeAgent("glm"),
        "deepseek": FakeAgent("deepseek", configured=False),
    }
    agents["deepseek"].status_note = "缺少密钥"
    bot, transport = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("/agent status"))
    joined = "\n".join(transport.texts())
    assert "✅ 已配置" in joined
    assert "❌ 缺少密钥" in joined
    assert "GLM" in joined and "DEEPSEEK" in joined
    # 单个 Agent 的状态
    await bot.handle_message(make_message("/agent status glm"))
    assert "GLM" in transport.texts()[-1]


async def test_disabled_backend_rejected_before_model_call(tmp_path):
    """已禁用的后端在调用前被明确拒绝，不降级、不换后端。"""
    settings = make_settings(tmp_path)
    agents = {"gpt": FakeAgent("gpt", enabled=False)}
    bot, transport = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("/task gpt -- 你好"))
    assert agents["gpt"].calls == []
    assert any("已禁用" in t for t in transport.texts())


async def test_codex_command_returns_not_configured(tmp_path, monkeypatch):
    """真实 CodexAdapter：未配置时明确返回，不启动 CLI、不伪造执行（F16）。

    显式隔离：不存在的 binary + 空的 CODEX_HOME（无 auth.json/models_cache.json），
    因此无论本机是否安装并登录 Codex，这里都不会真的启动它。
    """
    from app.agents.codex import CodexAdapter

    isolated_home = tmp_path / "codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    adapter = CodexAdapter(
        True, binary="definitely-not-installed-codex",
        cache_path=isolated_home / "models_cache.json",
    )
    assert adapter.configured is False
    assert adapter.binary is None or not adapter.model_options

    settings = make_settings(tmp_path)
    bot, transport = build_dispatcher(settings, {"codex": adapter})
    await bot.handle_message(make_message("/task codex -- 检查这个项目"))
    joined = "\n".join(transport.texts())
    assert "Codex" in joined
    assert "尚未配置" in joined or "未配置" in joined


async def test_task_status_and_result_are_owner_scoped(tmp_path):
    """任务按「群 × 发起人」隔离：他人（即使已授权）看不到也控制不了。"""
    settings = make_settings(tmp_path, qq_allowed_user_ids=[100, 200])
    agents = {"glm": FakeAgent("glm")}
    bot, transport = build_dispatcher(settings, agents)

    await bot.handle_message(make_message("/task glm -- 我的问题", user_id=100))
    match = re.search(r"task ([0-9a-f]{8})", transport.texts()[-1])
    assert match is not None
    task_id = match.group(1)

    # 本人可见
    await bot.handle_message(make_message(f"/task status {task_id}", user_id=100))
    assert "succeeded" in transport.texts()[-1]

    # 他人看不到、取不到、也取消不了
    for command in (f"/task status {task_id}", f"/task result {task_id}", f"/task cancel {task_id}"):
        await bot.handle_message(make_message(command, user_id=200))
        assert "任务不存在或不属于你" in transport.texts()[-1]

    # 伪造 ID 同样不可见
    await bot.handle_message(make_message("/task result deadbeef", user_id=100))
    assert "任务不存在或不属于你" in transport.texts()[-1]


async def test_cq_code_in_message_is_stripped(tmp_path):
    """CQ 码属于不可信输入，必须在路由前剥离。"""
    settings = make_settings(tmp_path)
    agents = {"glm": FakeAgent("glm")}
    bot, _ = build_dispatcher(settings, agents)
    await bot.handle_message(make_message("[CQ:at,qq=123] /task glm -- 你好"))
    assert len(agents["glm"].calls) == 1
    assert agents["glm"].last_prompt() == "你好"


class TestRegistryParsing:
    @staticmethod
    def _preset(name: str):
        from app.commands.presets import PresetSpec

        return {name: PresetSpec(name=name, description="x", agent="glm", template="{task}")}

    """CommandRegistry.parse：注册命令、子命令、旧口令迁移提示、未知内容静默。"""

    def setup_method(self):
        self.registry = CommandRegistry()

    def test_plain_text(self):
        assert self.registry.parse("你好") is None

    def test_unknown_command_is_silent(self):
        assert self.registry.parse("/foo bar") is None
        assert self.registry.parse("/glmx 你好") is None

    def test_registered_task_keeps_agent_and_body(self):
        cmd = self.registry.parse("/task glm -- 你好 世界")
        assert cmd is not None and not isinstance(cmd, str)
        assert cmd.name == "task" and cmd.subcommand == ""
        assert cmd.rest == "glm -- 你好 世界"

    def test_subcommands_are_split(self):
        cmd = self.registry.parse("/agent models deepseek")
        assert cmd.subcommand == "models" and cmd.rest == "deepseek"
        cmd = self.registry.parse("/task status 1a2b3c4d")
        assert cmd.name == "task" and cmd.subcommand == "status" and cmd.rest == "1a2b3c4d"
        # 不是子命令时整段保留
        cmd = self.registry.parse("/agent deepseek")
        assert cmd.subcommand == "" and cmd.rest == "deepseek"

    def test_case_insensitive(self):
        cmd = self.registry.parse("/TASK GLM -- hi")
        assert cmd is not None and cmd.name == "task"

    def test_legacy_names_return_migration_text_instead_of_command(self):
        for name in LEGACY_COMMANDS:
            assert self.registry.get(name) is None, f"/{name} 不应再注册为命令"
            parsed = self.registry.parse(f"/{name} 参数")
            assert isinstance(parsed, str), f"/{name} 应返回迁移提示文本"
            assert "旧口令" in parsed and LEGACY_COMMANDS[name] in parsed
            assert not self.registry.is_legacy("task")

    def test_multiline_args(self):
        cmd = self.registry.parse("/task glm -- 第一行\n第二行")
        assert cmd is not None and cmd.args == "glm -- 第一行\n第二行"

    def test_preset_name_must_not_collide_with_legacy_or_reserved(self):
        with pytest.raises(ValueError):
            self.registry.register_presets(self._preset("glm"))  # 与旧口令冲突
        with pytest.raises(ValueError):
            self.registry.register_presets(self._preset("status"))  # 与保留子命令冲突
        self.registry.register_presets(self._preset("review"))  # 合法 preset 名
        assert "review" in self.registry.names()          # preset 进入同一份规格表
        assert "review" in self.registry.help_text("review")
