#!/usr/bin/env python3
"""从命令注册表生成口令清单（Markdown / JSON / 纯文本）。

用法：
    .venv/bin/python scripts/render_commands.py --format md > outputs/命令清单.md
    .venv/bin/python scripts/render_commands.py --format json
    .venv/bin/python scripts/render_commands.py --format text

清单内容全部来自 `app.commands.registry.CommandRegistry`，因此与
/help、/command list、HTTP /api/v1/commands 完全一致——不存在第二份手写列表。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backends.builtin import register_builtin_adapters  # noqa: E402
from app.commands.presets import presets_from_config  # noqa: E402
from app.commands.registry import LEGACY_COMMANDS, CommandRegistry  # noqa: E402


def render_md(registry: CommandRegistry, presets: dict) -> str:
    lines = [
        "# 命令清单（由注册表自动生成）",
        "",
        "> 本文件由 `scripts/render_commands.py` 生成，请勿手工修改；"
        "命令与参数以 `app/commands/registry.py` 与 `app/commands/spec.py` 为准。",
        "",
        "## 规范命令族",
        "",
        "| 命令 | 说明 | 权限 | 处理器 |",
        "|---|---|---|---|",
    ]
    for spec in registry.all_specs():
        lines.append(
            f"| `/{spec.name}` | {spec.description} | {spec.authorization} | "
            f"`{spec.handler_key}` |"
        )
    for spec in registry.all_specs():
        lines += ["", f"### /{spec.name}", "", f"用法：`{spec.usage}`", ""]
        if spec.subcommands:
            lines.append("子命令：" + "、".join(f"`{item}`" for item in spec.subcommands))
            lines.append("")
        if spec.options:
            lines.append("| 选项 | 说明 | 取值 |")
            lines.append("|---|---|---|")
            for option in spec.options:
                placeholder = f"`<{option.placeholder}>`" if option.takes_value else "（无值）"
                choices = "、".join(f"`{item}`" for item in option.choices) if option.choices else "—"
                lines.append(f"| `--{option.name}` | {option.description} | {placeholder} {choices} |")
            lines.append("")
        if spec.examples:
            lines.append("示例：")
            lines += [f"- `{item}`" for item in spec.examples]
            lines.append("")
        if spec.notes:
            lines.append("说明：")
            lines += [f"- {item}" for item in spec.notes]
            lines.append("")
    lines += ["## 自定义 preset（来自 commands.presets）", ""]
    if not presets:
        lines.append("（未配置 preset）")
    else:
        lines.append("| 口令 | 说明 | 固定 Agent | 固定模式 | 可覆盖 |")
        lines.append("|---|---|---|---|---|")
        for name, preset in presets.items():
            lines.append(
                f"| `/{name}` | {preset.description} | `{preset.agent}` | "
                f"`{preset.mode}` | {'、'.join(preset.allow_overrides) or '（无）'} |"
            )
    lines += [
        "",
        "## 旧口令（已停用，只返回迁移提示，不执行任何操作）",
        "",
        "| 旧口令 | 替代用法 |",
        "|---|---|",
    ]
    for name, replacement in LEGACY_COMMANDS.items():
        lines.append(f"| `/{name}` | {replacement} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成统一控制台命令清单")
    parser.add_argument("--format", choices=("md", "json", "text"), default="md")
    parser.add_argument("--config", help="可选：读取 YAML 以获得 commands.presets")
    args = parser.parse_args(argv)

    register_builtin_adapters()
    registry = CommandRegistry()
    presets: dict = {}
    if args.config:
        import yaml

        raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        presets = presets_from_config((raw.get("commands") or {}).get("presets"))

    if args.format == "md":
        print(render_md(registry, presets))
    elif args.format == "json":
        print(json.dumps(
            {
                "commands": registry.discovery(),
                "presets": [preset.discovery() for preset in presets.values()],
                "legacy": LEGACY_COMMANDS,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(registry.help_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
