"""命令层：注册表、公共选项解析与 preset 展开。

本包不导入调度器或存储，保证"命令语法"与"业务执行"解耦：
`/help`、`/command`、HTTP discovery 与解析校验都由注册表生成。
"""

from app.commands.options import (
    MODE_EDIT,
    MODE_EXEC,
    MODE_READ,
    MODE_TEXT,
    MODES,
    PUBLIC_TASK_OPTIONS,
    TEAM_EXTRA_OPTIONS,
    OptionError,
    PublicOptions,
    parse_public_options,
    parse_task_arguments,
    split_body,
)
from app.commands.presets import (
    ExpandedPreset,
    PresetError,
    PresetSpec,
    expand_preset,
    presets_from_config,
)
from app.commands.registry import (
    DEFAULT_SPECS,
    LEGACY_COMMANDS,
    MIGRATION_TEXT,
    CommandRegistry,
    ParsedCommand,
)
from app.commands.spec import AUTH_ADMIN, AUTH_MEMBER, CommandSpec, OptionSpec

__all__ = [
    "AUTH_ADMIN",
    "AUTH_MEMBER",
    "CommandRegistry",
    "CommandSpec",
    "DEFAULT_SPECS",
    "ExpandedPreset",
    "LEGACY_COMMANDS",
    "MIGRATION_TEXT",
    "MODE_EDIT",
    "MODE_EXEC",
    "MODE_READ",
    "MODE_TEXT",
    "MODES",
    "OptionError",
    "OptionSpec",
    "PUBLIC_TASK_OPTIONS",
    "ParsedCommand",
    "PresetError",
    "PresetSpec",
    "PublicOptions",
    "TEAM_EXTRA_OPTIONS",
    "expand_preset",
    "parse_public_options",
    "parse_task_arguments",
    "presets_from_config",
    "split_body",
]
