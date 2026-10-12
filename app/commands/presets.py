"""常用口令 preset：把固定任务模板展开为规范任务请求。

约束（统一架构第 8 节）：
- preset 只做文本替换，不 eval、不生成 Shell、不解析正文里的任何命令；
- preset 声明的 agent/mode/backend 不能超过调用者自身的 ACL——最终权限仍是
  「用户 ACL ∩ preset 声明 ∩ 后端能力 ∩ 全局策略」；
- 调用者只能覆盖 preset 明确允许的公共选项（默认只允许 --dir/--find 与正文）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.commands.options import MODES, PublicOptions

_PLACEHOLDER = "{task}"


class PresetError(ValueError):
    """preset 配置或调用错误；message 可直接回执。"""


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """一个注册在 `commands.presets` 里的常用口令。"""

    name: str
    description: str
    agent: str
    template: str
    mode: str = "text"
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    # 允许调用者覆盖的公共选项名（agent/mode/backend 默认不可覆盖）；
    # None = 按 mode 取默认：read/edit/exec 允许 --dir/--find，text 不允许
    allow_overrides: tuple[str, ...] | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("-", "").replace("_", "").isalnum():
            raise PresetError(f"preset 名称只能是字母/数字/-/_：{self.name!r}")
        if self.mode not in MODES:
            raise PresetError(f"preset {self.name} 的 mode 非法：{self.mode}")
        if not self.agent:
            raise PresetError(f"preset {self.name} 必须指定 agent")
        if _PLACEHOLDER not in self.template:
            raise PresetError(f"preset {self.name} 的模板必须包含 {_PLACEHOLDER} 占位符")
        if self.allow_overrides is None:
            object.__setattr__(
                self, "allow_overrides",
                () if self.mode == "text" else ("dir", "find"),
            )
        if self.mode == "text" and {"dir", "find"} & set(self.allow_overrides):
            # 允许覆盖目录选择器时，模式必须显式 ≥ read，否则模板自相矛盾
            raise PresetError(
                f"preset {self.name} 允许目录选项时 mode 不能是 text，请改成 read/edit/exec"
            )

    def discovery(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "agent": self.agent,
            "mode": self.mode,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
            "allow_overrides": list(self.allow_overrides),
            "template": self.template,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ExpandedPreset:
    """preset 展开结果：与 /task 完全相同的结构化字段。"""

    preset: str
    agent: str
    mode: str
    body: str
    directory: str | None = None
    find: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    effort_set: bool = False


def expand_preset(spec: PresetSpec, options: PublicOptions) -> ExpandedPreset:
    """把用户输入代入 preset 模板；越权覆盖一律拒绝。"""
    given = set(options.given)
    forbidden = sorted(
        name for name in given
        if name not in set(spec.allow_overrides) and name not in {"mode"}
    )
    if forbidden:
        allowed = "、".join(f"--{name}" for name in sorted(spec.allow_overrides)) or "（无）"
        raise PresetError(
            f"preset /{spec.name} 不允许覆盖：{'、'.join('--' + name for name in forbidden)}；"
            f"可覆盖：{allowed}；正文直接写在 `--` 之后。"
        )
    if options.mode is not None and options.mode != spec.mode:
        raise PresetError(
            f"preset /{spec.name} 固定为 {spec.mode} 模式，不能改成 {options.mode}。"
        )
    if not options.body.strip():
        raise PresetError(f"preset /{spec.name} 需要任务正文：/{spec.name} -- <正文>")
    body = spec.template.replace(_PLACEHOLDER, options.body.strip())
    return ExpandedPreset(
        preset=spec.name,
        agent=spec.agent,
        mode=spec.mode,
        body=body,
        directory=options.directory,
        find=options.find,
        backend=spec.backend,
        model=options.model or spec.model,
        effort=options.effort if options.effort_set else spec.effort,
        effort_set=options.effort_set or spec.effort is not None,
    )


def preset_command_spec(preset: PresetSpec, base_spec):
    """把 preset 表述成同一份 CommandSpec，供注册表/帮助/discovery 统一使用。"""
    from app.commands.spec import CommandSpec

    allowed = [
        option for option in base_spec.options if option.name in set(preset.allow_overrides)
    ]
    return CommandSpec(
        name=preset.name,
        description=preset.description,
        usage=f"/{preset.name} [选项] -- <正文>",
        handler_key="task",
        options=tuple(allowed),
        examples=(f"/{preset.name} -- <正文>",),
        notes=(
            f"preset：固定 Agent {preset.agent} · 固定模式 {preset.mode}",
            "模板只做文本替换，不 eval、不生成 Shell，也不会超过你自己的授权。",
        ),
    )


def presets_from_config(raw: object) -> dict[str, PresetSpec]:
    """解析 `commands.presets` 配置；非法条目在启动时明确报错。"""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise PresetError("commands.presets 必须是映射（名字 -> 定义）")
    presets: dict[str, PresetSpec] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            raise PresetError(f"preset {name} 的定义必须是映射")
        unknown = set(value) - {
            "description", "agent", "template", "mode", "backend", "model",
            "effort", "allow_overrides", "version",
        }
        if unknown:
            raise PresetError(f"preset {name} 含未知字段：{', '.join(sorted(unknown))}")
        spec = PresetSpec(
            name=str(name),
            description=str(value.get("description") or f"自定义口令 {name}"),
            agent=str(value.get("agent") or ""),
            template=str(value.get("template") or ""),
            mode=str(value.get("mode") or "text"),
            backend=(str(value["backend"]) if value.get("backend") else None),
            model=(str(value["model"]) if value.get("model") else None),
            effort=(str(value["effort"]) if value.get("effort") else None),
            allow_overrides=(
                tuple(value["allow_overrides"]) if value.get("allow_overrides") is not None
                else None
            ),
            version=int(value.get("version") or 1),
        )
        presets[spec.name] = spec
    return presets
