"""统一公共选项解析。

约束（统一架构第 3 节）：
- 所有入口（/task、/task all、/agent、/team add、HTTP）共用这一份解析与校验；
- 选项写在首个独立 `--` 之前，之后整段作为正文原样保留（引号、空行、中文、
  以 `--` 开头的内容都不再被解释）；
- 支持 `--key value`、`--key=value`、`"含 空格"` 与中文路径；
- 重复选项、缺值、未知选项、非法枚举、dir/find 冲突一律拒绝。

本模块不做权限判定，也不触碰文件系统。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.commands.spec import CommandSpec, OptionSpec

# 任务正文模式（与执行权限一一对应）
MODE_TEXT = "text"
MODE_READ = "read"
MODE_EDIT = "edit"
MODE_EXEC = "exec"
MODES = (MODE_TEXT, MODE_READ, MODE_EDIT, MODE_EXEC)

# 所有任务类入口共用的公共选项（顺序即帮助展示顺序）
PUBLIC_TASK_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec("backend", "指定执行后端 ID（默认取 Agent 的默认后端）", placeholder="后端ID"),
    OptionSpec("model", "指定模型（默认取你的保存选择或 Agent 默认）", placeholder="模型"),
    OptionSpec("effort", "推理强度；default 表示模型默认", placeholder="强度|default"),
    OptionSpec("mode", "执行权限：text 文本 / read 只读 / edit 读写 / exec 读写+受控命令",
               placeholder="text|read|edit|exec", choices=MODES),
    OptionSpec("dir", '任务目录：绝对路径或已绑定目录；"." 表示本群×你当前绑定的目录',
               placeholder="路径"),
    OptionSpec("find", "按关键词在授权搜索根内查找目录；返回候选后需选择", placeholder="关键词"),
)

# team add 在同一份公共选项之外只增加 --after
TEAM_EXTRA_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec("after", "本步的直接依赖步骤 ID，逗号分隔", placeholder="步骤ID,步骤ID"),
)

_USAGE_HINT = "参数用法：--model 模型 --effort 强度 --mode text|read|edit|exec --dir \"路径\" --find \"关键词\"；正文写在 `--` 之后。"


class OptionError(ValueError):
    """解析失败；message 可直接回执给用户。"""


@dataclass(frozen=True, slots=True)
class PublicOptions:
    """一次任务类命令解析出的公共选项与正文。"""

    body: str = ""
    model: str | None = None
    effort: str | None = None
    effort_set: bool = False
    backend: str | None = None
    mode: str | None = None
    directory: str | None = None
    find: str | None = None
    after: tuple[str, ...] = ()
    given: frozenset[str] = field(default_factory=frozenset)

    @property
    def mode_set(self) -> bool:
        return self.mode is not None

    @property
    def has_selector(self) -> bool:
        return self.directory is not None or self.find is not None

    def with_body(self, body: str) -> "PublicOptions":
        return PublicOptions(
            body=body, model=self.model, effort=self.effort, effort_set=self.effort_set,
            backend=self.backend, mode=self.mode, directory=self.directory,
            find=self.find, after=self.after, given=self.given,
        )

    def resolved_mode(self) -> str:
        """按规格推导最终模式：无目录选择器 → text；有 → read。"""
        if self.mode is not None:
            return self.mode
        return MODE_READ if self.has_selector else MODE_TEXT


def split_body(text: str) -> tuple[str, str | None]:
    """按首个独立 `--` 切出（选项区, 正文）。

    没有独立 `--` 时返回 ``(text, None)``，由调用方决定是否要求分隔符。
    独立 `--` = 前面是行首或空白，后面是行尾或空白。
    """
    raw = text or ""
    index = 0
    length = len(raw)
    while index < length:
        found = raw.find("--", index)
        if found < 0:
            return raw, None
        before_ok = found == 0 or raw[found - 1].isspace()
        after = found + 2
        after_ok = after >= length or raw[after].isspace()
        if before_ok and after_ok:
            return raw[:found], raw[after:]
        index = found + 2
    return raw, None


def _read_value(text: str, index: int, option: OptionSpec) -> tuple[str, int]:
    """从 index 起读取一个选项值；支持单/双引号包裹的空格与中文路径。"""
    length = len(text)
    while index < length and text[index] in " \t":
        index += 1
    if index >= length:
        raise OptionError(f"--{option.name} 缺少值。\n{_USAGE_HINT}")
    if text[index] in {'"', "'"}:
        quote = text[index]
        end = text.find(quote, index + 1)
        if end < 0:
            raise OptionError(f"--{option.name} 的引号没有闭合。\n{_USAGE_HINT}")
        value = text[index + 1:end]
        rest = end + 1
        if rest < length and not text[rest].isspace():
            raise OptionError(f"--{option.name} 的值后面还有无法识别的内容。\n{_USAGE_HINT}")
        return value, rest
    end = index
    while end < length and not text[end].isspace():
        end += 1
    return text[index:end], end


def parse_public_options(
    header: str,
    *,
    spec: CommandSpec,
    extra_options: tuple[OptionSpec, ...] = (),
    allow_after: bool = False,
) -> PublicOptions:
    """解析选项区文本；返回结构化公共选项（正文由调用方单独传入）。"""
    options: list[OptionSpec] = list(spec.options) + list(extra_options)
    known = {option.name: option for option in options}
    values: dict[str, str | None] = {}
    seen: set[str] = set()
    text = header or ""
    index = 0
    length = len(text)
    length_checked = False
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if not text.startswith("--", index):
            raise OptionError(
                f"无法识别的内容：{text[index:index + 24].split()[0] if text[index:].split() else char}"
                f"（公共选项必须写在 `--` 之前）。\n{_USAGE_HINT}"
            )
        index += 2
        name_end = index
        while name_end < length and (text[name_end].isalnum() or text[name_end] in "_-"):
            name_end += 1
        name = text[index:name_end].lower()
        if not name:
            raise OptionError(f"`--` 之后缺少选项名。\n{_USAGE_HINT}")
        option = known.get(name)
        if option is None or (name == "after" and not allow_after):
            allowed = ", ".join(f"--{item}" for item in sorted(known))
            raise OptionError(f"未知选项 --{name}；本命令支持：{allowed}。")
        if name in seen:
            raise OptionError(f"选项 --{name} 重复。")
        seen.add(name)
        index = name_end
        if option.takes_value:
            if index < length and text[index] == "=":
                index += 1
                if index < length and text[index] in {'"', "'"}:
                    value, index = _read_value(text, index, option)
                else:
                    end = index
                    while end < length and not text[end].isspace():
                        end += 1
                    value = text[index:end]
                    index = end
            else:
                value, index = _read_value(text, index, option)
            if value == "":
                raise OptionError(f"--{option.name} 缺少值。\n{_USAGE_HINT}")
            if value.startswith("--"):
                raise OptionError(f"--{option.name} 缺少值。\n{_USAGE_HINT}")
            if option.choices and value not in option.choices:
                raise OptionError(
                    f"--{option.name} 只支持：{'|'.join(option.choices)}（收到 {value}）。"
                )
            values[name] = value
        else:
            values[name] = None
            if index < length and text[index] == "=":
                raise OptionError(f"--{name} 不接受值。")
        length_checked = True
    del length_checked

    mode = values.get("mode")
    directory = values.get("dir")
    find = values.get("find")
    if directory is not None and find is not None:
        raise OptionError("--dir 与 --find 只能二选一。")
    if mode == MODE_TEXT and (directory is not None or find is not None):
        raise OptionError("--mode text 不接受 --dir/--find；需要目录权限请改用 read/edit/exec。")
    after: tuple[str, ...] = ()
    if "after" in values:
        after = _split_after(values.get("after") or "")
    return PublicOptions(
        model=values.get("model"),
        effort=values.get("effort"),
        effort_set="effort" in seen,
        backend=values.get("backend"),
        mode=mode,
        directory=directory,
        find=find,
        after=after,
        given=frozenset(seen),
    )


# 步骤 ID 形态与 app/workflows/models.STEP_ID_RE 保持一致（命令层不反向依赖工作流层）
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _split_after(value: str) -> tuple[str, ...]:
    items = [item.strip() for item in value.replace("，", ",").split(",")]
    if not value.strip() or any(not item for item in items):
        raise OptionError("--after 不能为空，且不能包含空项。")
    for item in items:
        if not _STEP_ID_RE.match(item):
            raise OptionError(
                f"--after 中的步骤 ID 无效：{item}（只能是小写字母开头的 1-32 位 [a-z0-9_-]）"
            )
    ordered: list[str] = []
    for item in items:
        if item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def parse_task_arguments(
    args: str,
    *,
    spec: CommandSpec,
    require_body: bool = True,
    allow_after: bool = False,
    extra_options: tuple[OptionSpec, ...] = (),
) -> PublicOptions:
    """解析 `<公共选项> -- <正文>`；返回含正文的结构化结果。"""
    header, body = split_body(args)
    if body is None:
        stripped = (args or "").strip()
        if "--" in stripped.replace("--", "--", 1) and _looks_like_option(stripped):
            raise OptionError(
                "检测到 `--` 选项但缺少正文分隔符；请把正文写在单独的 `--` 之后。\n" + _USAGE_HINT
            )
        header, body = args or "", ""
    parsed = parse_public_options(
        header, spec=spec, extra_options=extra_options, allow_after=allow_after
    )
    text = (body or "").strip()
    if require_body and not text:
        raise OptionError("任务正文为空；请写在 `--` 之后。")
    return PublicOptions(
        body=text,
        model=parsed.model,
        effort=parsed.effort,
        effort_set=parsed.effort_set,
        backend=parsed.backend,
        mode=parsed.mode,
        directory=parsed.directory,
        find=parsed.find,
        after=parsed.after,
        given=parsed.given,
    )


def _looks_like_option(text: str) -> bool:
    """正文缺失时判断用户是否其实写了选项（用于给出可理解的提示）。"""
    for token in text.split():
        if token.startswith("--") and len(token) > 2 and token[2].isalpha():
            return True
    return False
