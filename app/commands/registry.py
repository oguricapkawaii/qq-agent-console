"""命令注册表：集中登记语法、参数、权限、处理器键、帮助与示例。

- 启动时检测命名冲突（命令名重复、保留名与子命令冲突、未登记的 handler）；
- `/help`、`/command list|show`、HTTP `/api/v1/commands` 全部由注册表生成，
  不再维护第二份硬编码列表；
- 旧口令（/glm、/read、/all ...）不注册为命令，只保留"迁移提示"映射：
  它们绝不执行任何操作，也不调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.commands.options import PUBLIC_TASK_OPTIONS, TEAM_EXTRA_OPTIONS
from app.commands.spec import (
    AUTH_ADMIN,
    AUTH_MEMBER,
    CommandSpec,
    OptionSpec,
)

# ---------- 旧口令 → 迁移提示（拒绝提示，不是兼容执行层） ----------

LEGACY_COMMANDS: dict[str, str] = {
    "glm": "/task glm -- <问题>",
    "deepseek": "/task deepseek [选项] -- <问题>",
    "zcode": "/task zcode [选项] -- <问题>",
    "gpt": "/task gpt [选项] -- <问题>",
    "codex": "/task codex [选项] -- <问题>",
    "all": "/task all -- <问题>（仅文本并发，不承担团队协作）",
    "read": '/task <agent> --dir "路径" --mode read -- <问题>',
    "write": '/task <agent> --dir "路径" --mode edit -- <要求修改的内容>（确认码用 /confirm）',
    "models": "/agent models <agent>",
    "set": "/agent set <agent> [--model 模型] [--effort 强度]",
    "status": "/agent status [agent]（服务健康检查仍是 HTTP /status）",
    "clear": "/agent clear <agent>",
    "approve": "/confirm <确认码>",
    "whitelist": "/access add|remove|list",
}

MIGRATION_TEXT = (
    "⚠️ 旧口令 /{name} 已停用，且没有兼容执行层（不会读写文件、不会调用模型）。\n"
    "请改用：{replacement}\n"
    "发送 /help 查看全部新命令。"
)

# ---------- 规范命令族 ----------

_TASK = CommandSpec(
    name="task",
    description="统一任务入口：文本、读取目录、修改目录、受控命令执行",
    usage="/task <agent> [--backend ID] [--model 模型] [--effort 强度|default] "
          "[--mode text|read|edit|exec] [--dir \"路径\" | --find \"关键词\"] -- <任务正文>",
    handler_key="task",
    subcommands=("all", "status", "result", "cancel"),
    reserved_names=("all", "status", "result", "cancel"),
    options=PUBLIC_TASK_OPTIONS,
    authorization=AUTH_MEMBER,
    examples=(
        "/task deepseek -- 解释这段需求",
        "/task all -- 分别给出建议",
        '/task zcode --dir "/Users/example/Documents/项目" --mode edit -- 更新 README 的安装说明',
        '/task codex --find "记账项目" --mode exec -- 修复并运行测试',
        "/task status 1a2b3c4d",
        "/task result 1a2b3c4d",
        "/task cancel 1a2b3c4d",
    ),
    notes=(
        "不带目录选择器也没有 mode 时是纯文本；给出 --dir/--find 而省略 mode 时默认 read。",
        "--dir . 表示本群×你当前绑定的目录，不会回落到进程工作目录。",
        "正文里的任何内容都不会被当成权限；正文说“修改”也不会升级权限。",
    ),
)

_WORKSPACE = CommandSpec(
    name="workspace",
    description="授权目录的查找、绑定与状态查询",
    usage="/workspace find <关键词> | use \"绝对路径\" | status | clear | select <确认码> <序号>",
    handler_key="workspace",
    subcommands=("find", "use", "status", "clear", "select"),
    examples=(
        "/workspace find 记账项目",
        "/workspace select AB12CD34 1",
        '/workspace use "/Users/example/Documents/项目"',
        "/workspace status",
        "/workspace clear",
    ),
    notes=(
        "find 只在配置的授权搜索根内查找，限制深度、候选数与耗时，不扫描整机。",
        "即使只有一个候选也必须由本人 select；确认码绑定群、发起人、请求与 TTL。",
    ),
)

_AGENT = CommandSpec(
    name="agent",
    description="逻辑 Agent 与后端能力、模型选择和上下文管理",
    usage="/agent list | models <agent> | set <agent> [--model 模型] [--effort 强度|default] "
          "[--backend ID] | status [agent] | clear <agent>",
    handler_key="agent",
    subcommands=("list", "models", "set", "status", "clear"),
    options=(
        OptionSpec("model", "保存为该 Agent 的默认模型", placeholder="模型"),
        OptionSpec("effort", "保存为该 Agent 的默认强度；default 表示模型默认", placeholder="强度|default"),
        OptionSpec("backend", "保存为该 Agent 的默认后端", placeholder="后端ID"),
    ),
    examples=(
        "/agent list",
        "/agent models deepseek",
        "/agent set deepseek --model <可用模型> --effort <支持的强度>",
        "/agent set deepseek",
        "/agent status",
        "/agent clear deepseek",
    ),
    notes=(
        "模型与强度必须真实存在于该后端目录中，否则拒绝保存。",
        "目录授权不在这里保存：目录只有 /workspace 绑定与显式 --dir 两种来源。",
    ),
)

_TEAM = CommandSpec(
    name="team",
    description="多 Agent 团队协作（DAG、冻结计划、后台调度、结果、取消、重试）",
    usage="/team create <目标> | add <团队ID> <步骤ID> <agent> [选项] -- <本步任务> | "
          "remove <团队ID> <步骤ID> | plan <团队ID> | run <团队ID> | status [团队ID] | "
          "result <团队ID> [步骤ID] | cancel <团队ID> | retry <团队ID>",
    handler_key="team",
    subcommands=("create", "add", "remove", "plan", "run", "status", "result", "cancel", "retry"),
    options=PUBLIC_TASK_OPTIONS + TEAM_EXTRA_OPTIONS,
    examples=(
        "/team create 修复项目问题并验证",
        '/team add t1a2b3c4 inspect deepseek --mode read --dir "/Users/example/Documents/项目" -- 分析问题',
        '/team add t1a2b3c4 fix zcode --after inspect --mode edit --dir "/Users/example/Documents/项目" -- 修改文件',
        "/team plan t1a2b3c4",
        "/team run t1a2b3c4",
        "/team result t1a2b3c4",
        "/team retry t1a2b3c4",
    ),
    notes=(
        "add 的公共选项与 /task 完全相同，只多一个 --after；未写 mode/dir 的步骤默认纯文本。",
        "run 前冻结每步 backend/model/effort/mode/规范目录；未选完目录时计划保持 draft。",
        "同目录步骤互斥；需要确定先后顺序必须显式写 --after。",
    ),
)

_CONFIRM = CommandSpec(
    name="confirm",
    description="确认一项已被明确列出内容的受控操作",
    usage="/confirm <确认码>",
    handler_key="confirm",
    examples=("/confirm AB12CD34",),
    notes=("确认码一次性、绑定发起人并有 TTL；不是每次普通目录修改的必经步骤。",),
)

_ACCESS = CommandSpec(
    name="access",
    description="本群成员授权管理（仅管理员）",
    usage="/access add <QQ号> | remove <QQ号> | list",
    handler_key="access",
    subcommands=("add", "remove", "list"),
    authorization=AUTH_ADMIN,
    examples=("/access add 10001", "/access remove 10001", "/access list"),
    notes=("普通成员默认只能文本；目录读写与执行权限由配置的目录 ACL 决定。",),
)

_COMMAND = CommandSpec(
    name="command",
    description="发现已登记口令、参数与示例（不调用模型）",
    usage="/command list | show <名称>",
    handler_key="command",
    subcommands=("list", "show"),
    examples=("/command list", "/command show task"),
)

_HELP = CommandSpec(
    name="help",
    description="显示帮助；可指定命令族",
    usage="/help [命令族]",
    handler_key="help",
    examples=("/help", "/help team"),
)

DEFAULT_SPECS: tuple[CommandSpec, ...] = (
    _TASK, _WORKSPACE, _AGENT, _TEAM, _CONFIRM, _ACCESS, _COMMAND, _HELP,
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """解析后的命令：命令名、原始参数、可选子命令。"""

    name: str
    args: str
    subcommand: str = ""
    rest: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.subcommand}".strip()


@dataclass(slots=True)
class CommandRegistry:
    """命令规格注册表。所有入口共用同一实例。"""

    specs: dict[str, CommandSpec] = field(default_factory=dict)
    legacy: dict[str, str] = field(default_factory=lambda: dict(LEGACY_COMMANDS))
    handler_keys: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.specs:
            for spec in DEFAULT_SPECS:
                self.register(spec)

    # ---------- 注册 ----------

    def register(self, spec: CommandSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"命令名重复注册：/{spec.name}")
        if spec.name in self.legacy:
            raise ValueError(f"命令 /{spec.name} 与旧口令迁移表冲突，必须改名或删除旧条目")
        reserved = self.reserved_names()
        if spec.name in reserved:
            raise ValueError(
                f"命令名 /{spec.name} 与保留名（子命令）冲突，会让解析产生歧义"
            )
        self.specs[spec.name] = spec

    def reserved_names(self) -> frozenset[str]:
        names: set[str] = set()
        for spec in self.specs.values():
            names |= set(spec.reserved())
        return frozenset(names)

    def register_presets(self, presets) -> None:
        """登记 preset：与命令族同一份规格表（帮助/discovery/解析统一）。

        名字不得与命令名、保留名或旧口令冲突；preset 的规格直接进入 `specs`，
        因此 /help、/command、HTTP discovery 都会自动包含它。
        """
        from app.commands.presets import preset_command_spec

        base = self.specs.get("task")
        for name, preset in presets.items():
            if name in self.specs:
                raise ValueError(f"preset /{name} 与命令族同名")
            if name in self.reserved_names():
                raise ValueError(f"preset /{name} 使用了保留名（子命令），必须改名")
            if name in self.legacy:
                raise ValueError(f"preset /{name} 与旧口令迁移表冲突，必须改名")
            if base is None:  # pragma: no cover - 注册表必然包含 task
                raise ValueError("preset 需要先注册 task 命令族")
            self.specs[name] = preset_command_spec(preset, base)

    def register_handler(self, handler_key: str) -> None:
        self.handler_keys.add(handler_key)

    def bind_handlers(self, handler_keys) -> None:
        """由调度器在装配时登记已实现的 handler key。"""
        self.handler_keys = set(handler_keys)

    def verify(self) -> None:
        """启动自检：handler 必须都已实现，避免"注册了但没人处理"。"""
        missing = sorted(
            {spec.handler_key for spec in self.specs.values()} - self.handler_keys
        )
        if missing:
            raise ValueError(f"以下命令注册了未实现的处理器：{', '.join(missing)}")

    # ---------- 查询 ----------

    def get(self, name: str) -> CommandSpec | None:
        return self.specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.specs)

    def preset_names(self) -> tuple[str, ...]:
        """内置命令族之外的 preset 口令名。"""
        return tuple(
            name for name in self.specs if name not in {spec.name for spec in DEFAULT_SPECS}
        )

    def all_specs(self) -> list[CommandSpec]:
        return list(self.specs.values())

    def migration_hint(self, name: str) -> str | None:
        replacement = self.legacy.get(name)
        if replacement is None:
            return None
        return MIGRATION_TEXT.format(name=name, replacement=replacement)

    def is_legacy(self, name: str) -> bool:
        return name in self.legacy

    # ---------- 解析 ----------

    def parse(self, text: str) -> ParsedCommand | str | None:
        """把一条群消息解析为命令。

        返回：
        - ``ParsedCommand``：已登记命令；
        - ``str``：旧口令的迁移提示文案（调用方原样回执，绝不执行）；
        - ``None``：完全未知的内容（保持静默，普通聊天不触发任何 Agent）。
        """
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return None
        head, _, tail = stripped[1:].partition(" ")
        name = head.strip().lower()
        if not name:
            return None
        if name in self.legacy and name not in self.specs:
            return self.migration_hint(name) or ""
        spec = self.specs.get(name)
        if spec is None:
            return None
        args = tail.strip()
        subcommand = ""
        rest = args
        if spec.subcommands:
            token, _, remainder = args.partition(" ")
            candidate = token.strip().lower()
            if candidate in spec.subcommands:
                subcommand = candidate
                rest = remainder.strip()
        return ParsedCommand(name=spec.name, args=args, subcommand=subcommand, rest=rest)

    # ---------- 帮助 / 发现 ----------

    def discovery(self) -> list[dict]:
        return [spec.discovery() for spec in self.specs.values()]

    def help_text(self, family: str = "") -> str:
        family = (family or "").strip().lower().lstrip("/")
        if family:
            spec = self.specs.get(family)
            if spec is None:
                return f"未知命令族：{family}；可用：" + "、".join(self.names())
            return self._family_help(spec)
        lines = ["📖 可用命令（统一控制台）："]
        for spec in self.specs.values():
            lines.append(f"{spec.usage} — {spec.description}")
        lines.append("")
        lines.append("常用示例：")
        for example in self.specs["task"].examples[:4]:
            lines.append(f"· {example}")
        lines.append("")
        lines.append("说明：")
        lines.append("· 旧口令（/glm、/read、/write、/all、/models、/set、/status、/clear、/approve、/whitelist 等）已停用，只会返回迁移提示，不会执行任何操作。")
        lines.append("· 权限：普通成员默认文本；目录 read/edit/exec 需配置目录 ACL；access 管理仅管理员。")
        lines.append("· 任务按「群 × 发起人」隔离，管理员也不能查看或控制他人的任务。")
        lines.append("· 用 /command list 查看全部命令，/command show task 查看参数细节，/help team 查看团队协作。")
        return "\n".join(lines)

    def _family_help(self, spec: CommandSpec) -> str:
        lines = [f"📖 /{spec.name} — {spec.description}", f"用法：{spec.usage}"]
        if spec.subcommands:
            lines.append("子命令：" + "、".join(spec.subcommands))
        if spec.options:
            lines.append("公共选项：")
            for option in spec.options:
                placeholder = f" <{option.placeholder}>" if option.takes_value else ""
                choices = f"（{'|'.join(option.choices)}）" if option.choices else ""
                lines.append(f"  --{option.name}{placeholder} — {option.description}{choices}")
        if spec.examples:
            lines.append("示例：")
            lines.extend(f"  {example}" for example in spec.examples)
        if spec.notes:
            lines.append("说明：")
            lines.extend(f"  · {note}" for note in spec.notes)
        if spec.authorization == AUTH_ADMIN:
            lines.append("权限：仅机器人管理员。")
        return "\n".join(lines)
