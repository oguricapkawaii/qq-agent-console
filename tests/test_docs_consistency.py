"""A22：交付一致性——注册表 / 生成清单 / README / 操作指南 / HTTP discovery。

这份测试把"文档与实现一致"变成可执行断言，避免文档漂移：
- 四处的规范命令集合必须完全相同；
- 旧口令只能出现在"已停用/迁移"的语境里；
- `outputs/命令清单.md` 必须与 `scripts/render_commands.py` 的当前输出逐字一致；
- 配置文件示例中引用的路径与 preset 必须能被真实解析。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import ApiIdentity, create_router
from app.commands.presets import presets_from_config
from app.commands.registry import LEGACY_COMMANDS, CommandRegistry
from helpers import ConsoleHarness, FakeAgent, workspace_settings

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = {"task", "workspace", "agent", "team", "confirm", "access", "command", "help"}
LEGACY_CONTEXT = ("旧", "迁移", "停用", "不再", "已删", "提示", "已知", "差异", "以前")


def _mentions_command(text: str, name: str) -> bool:
    """判断文本里是否把 /name 当作"命令"提及（排除文件路径里的 /glm 之类）。"""
    return bool(re.search(rf"(?<![A-Za-z0-9./_])/{name}(?![A-Za-z0-9_])", text))


def _canonical_section(text: str) -> str:
    """只取"旧口令"章节之前的部分，避免把迁移表算成可用命令。"""
    for marker in ("## 旧口令", "旧口令（已停用"):
        index = text.find(marker)
        if index >= 0:
            return text[:index]
    return text


def test_registry_has_exactly_the_canonical_commands() -> None:
    registry = CommandRegistry()
    assert set(registry.names()) == CANONICAL
    for name in LEGACY_COMMANDS:
        assert registry.get(name) is None
        assert registry.migration_hint(name)


def test_generated_command_list_matches_registry() -> None:
    registry = CommandRegistry()
    text = (ROOT / "outputs/命令清单.md").read_text(encoding="utf-8")
    generated = set(re.findall(r"^\| `/([a-z]+)` \|", _canonical_section(text), flags=re.M))
    assert generated == set(registry.names())


def test_generated_command_list_is_up_to_date() -> None:
    """文件必须与生成脚本当前输出一致（改代码后忘了重新生成会在这里失败）。"""
    result = subprocess.run(
        [sys.executable, "scripts/render_commands.py", "--format", "md"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    current = (ROOT / "outputs/命令清单.md").read_text(encoding="utf-8")
    assert result.stdout.strip() == current.strip(), (
        "outputs/命令清单.md 已过期，请运行 "
        "`.venv/bin/python scripts/render_commands.py --format md > outputs/命令清单.md`"
    )


@pytest.mark.parametrize("doc", ["README.md", "outputs/操作指南.md"])
def test_docs_only_use_canonical_commands(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`/([a-z]+)", text))
    canonical_mentioned = mentioned & CANONICAL
    assert canonical_mentioned == CANONICAL, f"{doc} 未完整覆盖规范命令族"
    # 旧口令只允许出现在「已停用/迁移/已知差异」语境、所在小节标题含该类语境，
    # 或与替代用法同现（迁移表行）。HTTP /status 是保留的健康检查接口，单独豁免。
    heading = ""
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line
        for name in LEGACY_COMMANDS:
            if not _mentions_command(line, name):
                continue
            if name == "status" and (
                "healthz" in line or "HTTP" in line or "http://" in line or "健康" in line
            ):
                continue
            has_context = any(
                word in line or word in heading for word in LEGACY_CONTEXT
            )
            has_replacement = bool(re.search(r"`/(?:task|workspace|agent|team|confirm|access)\b", line))
            assert has_context or has_replacement, (
                f"{doc} 中的 /{name} 既未标注停用、也没有给出替代用法：{line.strip()[:80]}"
            )


async def test_http_discovery_matches_registry(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    app = FastAPI()
    app.state.dispatcher = harness.dispatcher
    app.state.api_tokens = {
        "t": ApiIdentity(name="t", user_id=100, group_ids=(200,), scopes=frozenset())
    }
    app.include_router(create_router(lambda target: target.state))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            response = await http.get(
                "/api/v1/commands", headers={"Authorization": "Bearer t"}
            )
        names = {item["name"] for item in response.json()["commands"]}
        assert names == CANONICAL
    finally:
        await harness.aclose()


async def test_qq_help_matches_registry(tmp_path) -> None:
    settings = workspace_settings(tmp_path)
    harness = ConsoleHarness(settings, {"deepseek": FakeAgent("deepseek")})
    try:
        help_text = await harness.command("/help")
        listing = await harness.command("/command list")
        for name in CANONICAL:
            assert f"/{name}" in help_text
            assert f"/{name}" in listing
        for name in LEGACY_COMMANDS:
            assert not re.search(rf"^/{name}\b", help_text, flags=re.M)
            assert not re.search(rf"^/{name}\b", listing, flags=re.M)
    finally:
        await harness.aclose()


def test_config_example_is_parseable_and_consistent() -> None:
    """config.example.yaml 必须能被真实解析：路径存在性、preset、adapter 类型。"""
    import os

    import yaml

    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    presets = presets_from_config((raw.get("commands") or {}).get("presets"))
    assert presets, "示例配置应至少包含一个 preset"
    # 示例里引用的 adapter 必须都已登记
    from app.backends.builtin import register_builtin_adapters
    from app.backends.factory import known_adapters

    register_builtin_adapters()
    for backend_id, value in (raw.get("backends") or {}).items():
        assert value["adapter"] in known_adapters(), backend_id
    # 示例默认不应授权任何目录（避免复制即扩大权限）
    assert (raw.get("workspaces") or {}).get("roots") == []
    # 示例里出现的路径要么存在，要么是注释里的占位
    for backend in (raw.get("backends") or {}).values():
        path = str(backend.get("binary") or "")
        if path and not path.startswith("/Applications"):
            assert os.path.exists(path) or path in {"codex"}, path


def test_env_example_has_no_real_secrets() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if not value:
            continue
        assert not re.search(r"sk-[A-Za-z0-9_\-]{8,}", value), f"{key} 疑似真实密钥"
        if re.search(r"(USER_IDS|GROUP_IDS|IGNORE_USER_IDS|USER_ID|GROUP_ID)$", key):
            assert not re.fullmatch(r"\d{6,}", value), f"{key} 疑似真实 ID"
