"""命令与选项的声明式规格。

设计目标（见 outputs/统一控制台方案/统一架构.md 第 8 节）：
- 语法、参数、权限、处理器键、帮助与示例集中声明，注册表统一生成
  `/help`、`/command`、HTTP discovery 与解析校验；
- 新增常用口令只需在 `commands.presets` 配置固定任务模板；
- 新增业务动作只需新增一个 handler + 一个 CommandSpec，不改解析核心。

本模块只描述"是什么"，不导入调度器、不执行任何操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 权限等级：普通授权成员 / 管理员
AUTH_MEMBER = "member"
AUTH_ADMIN = "admin"
_AUTH_LEVELS = (AUTH_MEMBER, AUTH_ADMIN)


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """一个可为 `--name` 形式的公共选项。"""

    name: str
    description: str
    takes_value: bool = True
    placeholder: str = ""
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:  # pragma: no cover - 声明期错误，启动即暴露
        if not self.name or self.name.startswith("-"):
            raise ValueError(f"选项名非法：{self.name!r}")
        if self.takes_value and not self.placeholder:
            raise ValueError(f"带值选项必须给出占位符：--{self.name}")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """一条规范命令（命令族）的完整规格。"""

    name: str
    description: str
    usage: str
    handler_key: str
    subcommands: tuple[str, ...] = ()
    # 与子命令同层、不允许被当作子命令使用的保留名（例如 task 的 status/result）
    reserved_names: tuple[str, ...] = ()
    options: tuple[OptionSpec, ...] = ()
    authorization: str = AUTH_MEMBER
    examples: tuple[str, ...] = ()
    version: int = 1
    # 追加到 /help <命令族> 的说明行
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name or self.name.startswith("/"):
            raise ValueError(f"命令名不能带斜杠：{self.name!r}")
        if self.authorization not in _AUTH_LEVELS:
            raise ValueError(f"未知权限等级：{self.authorization}")
        if not self.handler_key:
            raise ValueError(f"命令 {self.name} 缺少 handler_key")
        names = [option.name for option in self.options]
        if len(names) != len(set(names)):
            raise ValueError(f"命令 {self.name} 存在重复选项声明")
        if len(set(self.subcommands)) != len(self.subcommands):
            raise ValueError(f"命令 {self.name} 存在重复子命令声明")
        if len(set(self.reserved_names)) != len(self.reserved_names):
            raise ValueError(f"命令 {self.name} 存在重复保留名声明")

    def reserved(self) -> frozenset[str]:
        """全部不允许被其他命令/preset/agent 复用的名字。"""
        return frozenset(self.subcommands) | frozenset(self.reserved_names)

    def option(self, name: str) -> OptionSpec | None:
        for option in self.options:
            if option.name == name:
                return option
        return None

    def option_names(self) -> tuple[str, ...]:
        return tuple(option.name for option in self.options)

    def discovery(self) -> dict:
        """HTTP/`/command show` 共用的只读描述。"""
        return {
            "name": self.name,
            "description": self.description,
            "usage": self.usage,
            "subcommands": list(self.subcommands),
            "reserved_names": list(self.reserved_names),
            "authorization": self.authorization,
            "handler_key": self.handler_key,
            "version": self.version,
            "examples": list(self.examples),
            "options": [
                {
                    "name": option.name,
                    "description": option.description,
                    "takes_value": option.takes_value,
                    "placeholder": option.placeholder,
                    "choices": list(option.choices),
                }
                for option in self.options
            ],
        }
