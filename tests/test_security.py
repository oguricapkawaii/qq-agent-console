"""安全测试：密钥不泄露（日志/错误/回复）、限流、命令执行策略。

新契约：询问 Agent 用 `/task <agent> -- ...`；本机能力不再由 `tool_policy` 开关控制，
而是由 ACL（目录 read/edit/exec）+ ExecPolicy（受控命令白名单）逐次判定。
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import httpx
import pytest

from app.agents.openai_compat import OpenAICompatibleAdapter
from app.logging_setup import build_redacting_handler
from app.security.exec_policy import ExecDenied, ExecPolicy
from app.security.rate_limit import SlidingWindowRateLimiter
from app.security.redaction import SecretRedactor
from helpers import ConsoleHarness, FakeAgent, build_dispatcher, make_message, make_settings

SECRET = "sk-super-secret-123456"


def test_redactor_removes_registered_secret():
    redactor = SecretRedactor()
    redactor.register(SECRET)
    out = redactor.redact(f"请求 {SECRET} 失败")
    assert SECRET not in out
    assert "***REDACTED***" in out


def test_redactor_matches_unknown_sk_patterns():
    redactor = SecretRedactor()
    out = redactor.redact("key=sk-abcdefgh12345678 and sk-tiny")
    assert "sk-abcdefgh12345678" not in out
    assert "sk-tiny" in out  # 过短的形态不匹配


def test_redacting_log_handler_redacts_secrets():
    redactor = SecretRedactor()
    redactor.register(SECRET)
    stream = io.StringIO()
    handler = build_redacting_handler(redactor, stream=stream)
    logger = logging.getLogger("test.redact")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("调用 %s 失败", SECRET)
        logger.info(f"直接格式化 {SECRET}")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert SECRET not in out
    assert out.count("***REDACTED***") == 2


async def test_group_reply_never_contains_api_key(tmp_path):
    """即使模型输出意外带出密钥，发到群里的消息也必须脱敏。"""
    settings = make_settings(tmp_path)
    harness = ConsoleHarness(settings, {"glm": FakeAgent("glm", reply=f"泄露：{SECRET}")})
    harness.redactor.register(SECRET)

    out = await harness.command("/task glm -- 显示密钥")

    assert SECRET not in out
    assert "***REDACTED***" in out


async def test_error_message_has_no_key_headers_or_stack(tmp_path):
    """真实适配器失败时，群里的错误消息只有简短类别，不含密钥/请求头/堆栈。"""
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": f"internal {SECRET}"}})

    adapter = OpenAICompatibleAdapter(
        name="glm", display_name="GLM",
        base_url="https://api.test/v1", api_key=SECRET, model="glm-4",
        http_transport=httpx.MockTransport(handler),
    )
    bot, transport = build_dispatcher(settings, {"glm": adapter})
    await bot.handle_message(make_message("/task glm -- 你好"))

    joined = "\n".join(transport.texts())
    assert "HTTP 500" in joined
    assert SECRET not in joined
    assert "Authorization" not in joined
    assert "Traceback" not in joined


async def test_task_logs_do_not_leak_secrets(tmp_path, caplog):
    """任务日志（task_id/后端/状态/耗时）不包含用户输入里的密钥。"""
    user_secret = "sk-userinput-99999999"
    settings = make_settings(tmp_path)
    bot, _ = build_dispatcher(settings, {"glm": FakeAgent("glm")})
    with caplog.at_level(logging.INFO, logger="qqbot.tasks.service"):
        await bot.handle_message(make_message(f"/task glm -- 我的key是{user_secret}"))
    assert user_secret not in caplog.text
    assert "task=" in caplog.text
    assert "status=succeeded" in caplog.text


async def test_rate_limit_per_user(tmp_path):
    settings = make_settings(tmp_path, rate_limit_per_minute=1)
    glm = FakeAgent("glm")
    bot, transport = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 第一问"))
    await bot.handle_message(make_message("/task glm -- 第二问"))

    assert len(glm.calls) == 1
    assert any("请求过于频繁" in t for t in transport.texts())


async def test_rate_limit_is_per_user(tmp_path):
    settings = make_settings(tmp_path, rate_limit_per_minute=1, qq_allowed_user_ids=[100, 200])
    glm = FakeAgent("glm")
    bot, _ = build_dispatcher(settings, {"glm": glm})

    await bot.handle_message(make_message("/task glm -- 问1", user_id=100))
    await bot.handle_message(make_message("/task glm -- 问2", user_id=200))
    assert len(glm.calls) == 2


def test_rate_limiter_window():
    limiter = SlidingWindowRateLimiter(max_calls=2, window_seconds=0.05)
    assert limiter.allow("u")
    assert limiter.allow("u")
    assert not limiter.allow("u")
    assert limiter.allow("other")  # 键之间独立


def test_no_legacy_tool_policy_switch_exists():
    """本机能力不再由 'tool_policy' 开关决定：只有 ACL + ExecPolicy 两条边界。"""
    import app.security as security

    assert not hasattr(security, "TextOnlyToolPolicy")
    assert not hasattr(security, "ToolPolicy")
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.security.tool_policy")
    # 旧的独立文件读写模块也已合并进 WorkspaceService/公共 broker
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.security.file_access")
    # 旧硬编码命令解析器同样不复存在
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.routing.commands")


def test_exec_policy_denies_privilege_and_network_commands(tmp_path):
    """exec 的唯一实现是受控命令策略：提权与联网工具一律拒绝。"""
    policy = ExecPolicy()
    for argv in (["sudo", "ls"], ["curl", "https://x"], ["ssh", "host"],
                 ["wget", "-O", "out", "http://x"]):
        with pytest.raises(ExecDenied):
            policy.check(argv, workspace=tmp_path, cwd=tmp_path)


def test_exec_policy_confines_workspace_and_allowlist(tmp_path):
    policy = ExecPolicy()
    # 工作目录必须落在授权目录内
    with pytest.raises(ExecDenied):
        policy.check(["ls"], workspace=tmp_path, cwd=Path("/etc"))
    # 不在允许清单内的可执行文件被拒
    with pytest.raises(ExecDenied):
        policy.check(["definitely-not-allowed"], workspace=tmp_path, cwd=tmp_path)
    # 指向授权目录之外的路径参数被拒
    with pytest.raises(ExecDenied):
        policy.check(["git", "add", "/etc/passwd"], workspace=tmp_path, cwd=tmp_path)
    # 命中敏感文件名的参数被拒
    with pytest.raises(ExecDenied):
        policy.check(["cat", ".env"], workspace=tmp_path, cwd=tmp_path)
    # 允许清单内的只读命令通过
    policy.check(["ls"], workspace=tmp_path, cwd=tmp_path)


def test_exec_policy_env_is_minimal():
    policy = ExecPolicy()
    env = policy.env({"PATH": "/usr/bin", "DEEPSEEK_API_KEY": "sk-leak", "HOME": "/tmp"})
    assert "DEEPSEEK_API_KEY" not in env
    assert "sk-leak" not in json.dumps(env)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/tmp"
