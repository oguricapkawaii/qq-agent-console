"""`/team` 子命令解析。

设计约束：
- 固定子命令；`add` 的公共选项与 `/task` **完全共用**一份解析器
  （app.commands.options），只额外支持 `--after`；
- `add` 先按首个独立 `--` 切出正文，正文原文保留（中文、引号、换行、以 -- 开头的内容）；
- 解析结果只作为数据结构返回，绝不执行 Shell，也不把正文字符串交给任何命令解析；
- V1 旧计划没有 mode/backend/dir 字段时按 text 解释（不授予文件权限）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.commands.options import (
    TEAM_EXTRA_OPTIONS,
    OptionError,
    parse_public_options,
    split_body,
)
from app.commands.registry import CommandRegistry
from app.workflows.models import validate_agent_name, validate_step_id, validate_team_id

USAGE = (
    "用法：\n"
    "/team create <目标>\n"
    "/team add <团队ID> <步骤ID> <agent> [--after a,b] [--backend ID] [--model 模型] "
    "[--effort 强度|default] [--mode text|read|edit|exec] [--dir \"路径\" | --find \"关键词\"] -- <本步任务>\n"
    "/team remove <团队ID> <步骤ID>\n"
    "/team plan <团队ID>\n"
    "/team run <团队ID>\n"
    "/team status [团队ID]\n"
    "/team result <团队ID> [步骤ID]\n"
    "/team cancel <团队ID>\n"
    "/team retry <团队ID>\n"
    "说明：不写 mode/dir/find 的步骤是纯文本；给出 --dir/--find 而省略 mode 时默认 read。"
)

_ACTIONS = ("create", "add", "remove", "plan", "run", "status", "result", "cancel", "retry")

_TEAM_SPEC_CACHE: tuple = ()


def _team_spec():
    """取得 team 命令规格（用于共享选项解析）。"""
    global _TEAM_SPEC_CACHE
    if not _TEAM_SPEC_CACHE:
        spec = CommandRegistry().get("team")
        if spec is None:  # pragma: no cover - 注册表必然包含 team
            raise RuntimeError("team 命令未注册")
        _TEAM_SPEC_CACHE = (spec,)
    return _TEAM_SPEC_CACHE[0]


@dataclass(frozen=True, slots=True)
class TeamCommand:
    action: str
    goal: str = ""
    team_id: str = ""
    step_id: str = ""
    agent: str = ""
    instruction: str = ""
    after: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    effort_set: bool = False
    result_step: str = ""
    mode: str | None = None
    backend: str | None = None
    directory: str | None = None
    find: str | None = None

    @property
    def has_capability_request(self) -> bool:
        return (
            self.mode is not None or self.directory is not None or self.find is not None
            or self.backend is not None
        )


def parse_team_command(
    args: str, *, max_prompt_chars: int, max_steps: int | None = None
) -> TeamCommand:
    """解析 `/team` 之后的参数；非法输入抛 ValueError（文案可直接回执）。"""
    del max_steps  # 步骤总数由 service 依据当前草稿判断
    text = (args or "").strip()
    if not text:
        raise ValueError(USAGE)
    head, _, rest = text.partition(" ")
    action = head.lower()
    if action not in _ACTIONS:
        raise ValueError(f"未知的 /team 子命令：{head}\n{USAGE}")
    rest = rest.strip()
    if action == "create":
        return _parse_create(rest, max_prompt_chars=max_prompt_chars)
    if action == "add":
        return _parse_add(rest, max_prompt_chars=max_prompt_chars)
    if action == "remove":
        return _parse_remove(rest)
    if action in {"plan", "run", "cancel", "retry"}:
        return TeamCommand(action=action, team_id=_require_team_id(rest, action))
    if action == "status":
        if not rest:
            return TeamCommand(action="status")
        return TeamCommand(action="status", team_id=_require_team_id(rest, action))
    parts = rest.split()
    if not parts:
        raise ValueError("用法：/team result <团队ID> [步骤ID]")
    team_id = _require_team_id(parts[0], "result")
    if len(parts) > 2:
        raise ValueError("用法：/team result <团队ID> [步骤ID]")
    step_id = ""
    if len(parts) == 2:
        if not validate_step_id(parts[1]):
            raise ValueError("步骤 ID 只能是小写字母开头的 1-32 位 [a-z0-9_-]，且不能是保留名。")
        step_id = parts[1]
    return TeamCommand(action="result", team_id=team_id, result_step=step_id)


def _parse_create(rest: str, *, max_prompt_chars: int) -> TeamCommand:
    if not rest:
        raise ValueError("用法：/team create <目标>")
    if len(rest) > max_prompt_chars:
        raise ValueError(f"团队目标过长（上限 {max_prompt_chars} 字）。")
    return TeamCommand(action="create", goal=rest)


def _require_team_id(value: str, action: str) -> str:
    tokens = value.split()
    token = tokens[0] if tokens else ""
    if not validate_team_id(token):
        raise ValueError(f"团队 ID 无效；用法：/team {action} <团队ID>")
    if len(tokens) > 1:
        raise ValueError(f"用法：/team {action} <团队ID>")
    return token


def _parse_add(rest: str, *, max_prompt_chars: int) -> TeamCommand:
    if not rest:
        raise ValueError("用法：/team add <团队ID> <步骤ID> <agent> [选项] -- <本步任务>")
    header_text, body = split_body(rest)
    if body is None:
        raise ValueError("缺少 `--` 分隔符；本步任务正文必须写在 `--` 之后。\n" + USAGE)
    head = header_text.split(None, 3)
    if len(head) < 3:
        raise ValueError("用法：/team add <团队ID> <步骤ID> <agent> [选项] -- <本步任务>")
    team_id, step_id, agent = head[0], head[1], head[2].lower()
    option_text = head[3] if len(head) > 3 else ""
    if not validate_team_id(team_id):
        raise ValueError("团队 ID 无效（形如 t7a91c20d）。")
    if not validate_step_id(step_id):
        raise ValueError("步骤 ID 只能是小写字母开头的 1-32 位 [a-z0-9_-]。")
    if not validate_agent_name(agent):
        raise ValueError("Agent 名称无效；可用 /agent list 查看已配置的 Agent。")
    if team_id == step_id:
        raise ValueError("步骤 ID 不能与团队 ID 相同。")
    try:
        options = parse_public_options(
            option_text, spec=_team_spec(), extra_options=TEAM_EXTRA_OPTIONS, allow_after=True
        )
    except OptionError as exc:
        raise ValueError(str(exc)) from None
    text = (body or "").strip()
    if not text:
        raise ValueError("本步任务正文为空；请写在 `--` 之后。")
    if len(text) > max_prompt_chars:
        raise ValueError(f"本步任务过长（上限 {max_prompt_chars} 字）。")
    return TeamCommand(
        action="add", team_id=team_id, step_id=step_id, agent=agent, instruction=text,
        after=options.after, model=options.model,
        effort=None if options.effort == "default" else options.effort,
        effort_set=options.effort_set, mode=options.mode, backend=options.backend,
        directory=options.directory, find=options.find,
    )


def _parse_remove(rest: str) -> TeamCommand:
    parts = rest.split()
    if len(parts) != 2:
        raise ValueError("用法：/team remove <团队ID> <步骤ID>")
    team_id, step_id = parts
    if not validate_team_id(team_id):
        raise ValueError("团队 ID 无效（形如 t7a91c20d）。")
    if not validate_step_id(step_id):
        raise ValueError("步骤 ID 只能是小写字母开头的 1-32 位 [a-z0-9_-]。")
    return TeamCommand(action="remove", team_id=team_id, step_id=step_id)
