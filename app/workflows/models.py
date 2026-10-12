"""协作任务的模型、状态机与纯函数校验。

不 import 任何适配器或存储实现，便于单测与复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 团队 ID：程序生成的固定形态，只当字符串查询，绝不拼接文件路径。
TEAM_ID_RE = re.compile(r"^t[0-9a-f]{8}$")
# 步骤 ID：小写字母开头，后接小写字母/数字/下划线/连字符。
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Agent 名称：与注册表命名保持一致。
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class TeamStatus:
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    ALL = (DRAFT, QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED)
    ACTIVE = (QUEUED, RUNNING)


class StepStatus:
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"

    ALL = (PENDING, READY, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, BLOCKED)
    # 本轮不会再变化的状态
    TERMINAL = (SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, BLOCKED)
    UNSCHEDULED = (PENDING, READY)


def validate_step_id(value: str) -> bool:
    return bool(STEP_ID_RE.match(value or ""))


def validate_team_id(value: str) -> bool:
    return bool(TEAM_ID_RE.match(value or ""))


def validate_agent_name(value: str) -> bool:
    return bool(AGENT_NAME_RE.match(value or ""))


@dataclass(frozen=True, slots=True)
class FrozenChoice:
    """run 时冻结的模型选择。

    effort_set 区分「用户显式写了 --effort（含 default）」与「未提供」：
    - effort_set=True：使用 effort（None 表示模型默认）；
    - effort_set=False：沿用 /set 偏好，但换模型后回到新模型默认。
    """

    model: str
    effort: str | None = None
    effort_set: bool = False


@dataclass(frozen=True, slots=True)
class StepSpec:
    """草稿中的一步定义（用户可编辑）。

    权限扩展（统一架构第 7 节）：每步可声明 mode（text/read/edit/exec）、
    backend 与目录；未声明的旧计划按 text 解释。
    """

    step_id: str
    seq: int
    agent: str
    instruction: str
    depends_on: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    effort_set: bool = False
    mode: str = "text"
    backend: str | None = None
    workspace: str | None = None
    # 仍需用户选择的目录关键词（未选定前计划保持 draft）
    find_keyword: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenStep:
    """冻结快照中的一步：后端/模型/强度/模式/目录与依赖都不再受后续变化影响。"""

    step_id: str
    seq: int
    agent: str
    instruction: str
    depends_on: tuple[str, ...]
    choice: FrozenChoice
    mode: str = "text"
    backend: str | None = None
    workspace: str | None = None
    # 冻结时该目录的授权引用（用于 plan 展示与审计，不参与实时判定）
    grant_label: str = ""

    @property
    def needs_workspace(self) -> bool:
        return self.mode in {"read", "edit", "exec"}

    def workspace_path(self):
        from pathlib import Path

        return Path(self.workspace) if self.workspace else None


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    team_id: str
    goal: str
    steps: tuple[FrozenStep, ...]

    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)


@dataclass(slots=True)
class WorkflowRecord:
    """team_workflows 的一行。"""

    team_id: str
    group_id: int
    owner_user_id: int
    goal: str
    status: str
    round: int
    created_at: float
    updated_at: float


@dataclass(slots=True)
class StepRunRecord:
    """team_step_runs 的一行。"""

    team_id: str
    round: int
    step_id: str
    status: str
    answer: str = ""
    error: str | None = None
    source_round: int | None = None
    task_id: str | None = None
    attempts: int = 0
    model: str = ""
    effort: str | None = None
    duration_ms: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    # 文件步骤：已落盘变更的指纹（path:after_hash 排序后的 sha256）
    change_fingerprint: str = ""
    changed_files: int = 0
    # exec 步骤实际运行过的命令（用于判断"是否可安全重复"）
    commands: tuple[str, ...] = ()
    # 失败详情（例如输入超限的具体字节数与上限），用于结果展示
    detail: str = ""
    # F10：成功文件步骤读取过的输入文件 + 当时哈希（retry 时判定输入漂移）
    input_fingerprint: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RunRecord:
    """team_runs 的一行。"""

    team_id: str
    round: int
    status: str
    plan_json: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UpstreamResult:
    """直接上游已经成功的答复；作为不可信数据注入本步输入。"""

    step_id: str
    agent: str
    answer: str
    status: str = StepStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """一次单步执行的结果（是否落库由 runner 决定）。"""

    ok: bool
    status: str
    answer: str = ""
    error: str | None = None
    detail: str = ""
    duration_ms: int = 0
    task_id: str = ""
    model: str = ""
    effort: str | None = None
    thinking: str = ""
    tool_log: tuple[str, ...] = ()
    backend: str = ""
    mode: str = "text"
    workspace: str | None = None
    change_fingerprint: str = ""
    changed_files: int = 0
    commands: tuple[str, ...] = ()
    # F10：本步读取过的输入文件与哈希
    input_fingerprint: dict[str, str] = field(default_factory=dict)
    # 是否可能已产生无法安全重复的副作用（部分写入/非幂等命令）
    untrusted_side_effects: bool = False


def topological_layers(
    step_ids: list[str], dependencies: dict[str, set[str]]
) -> list[list[str]] | None:
    """Kahn 拓扑分层；依赖未知、自环或环时返回 None。

    分层内保持输入顺序（调用方按 seq 排序传入），输出稳定。
    """
    known = list(step_ids)
    known_set = set(known)
    for step in known:
        for dep in dependencies.get(step, set()):
            if dep not in known_set:
                return None
    indegree = {step: 0 for step in known}
    children: dict[str, list[str]] = {step: [] for step in known}
    for step in known:
        for dep in sorted(dependencies.get(step, set())):
            indegree[step] += 1
            children[dep].append(step)
    layers: list[list[str]] = []
    ready = [step for step in known if indegree[step] == 0]
    seen = 0
    while ready:
        layers.append(list(ready))
        nxt: list[str] = []
        for step in ready:
            seen += 1
            for child in children[step]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        ready = nxt
    if seen != len(known):
        return None
    return layers


def find_undefined_dependencies(
    step_ids: list[str], dependencies: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """返回 [(步骤ID, 未定义依赖ID)]，按步骤顺序稳定输出。"""
    known = set(step_ids)
    missing: list[tuple[str, str]] = []
    for step in step_ids:
        for dep in sorted(dependencies.get(step, set())):
            if dep not in known:
                missing.append((step, dep))
    return missing


def dependents_map(
    step_ids: list[str], dependencies: dict[str, set[str]]
) -> dict[str, list[str]]:
    """反向依赖：dep -> [直接下游]，按输入顺序稳定。"""
    children: dict[str, list[str]] = {step: [] for step in step_ids}
    for step in step_ids:
        for dep in sorted(dependencies.get(step, set())):
            children.setdefault(dep, []).append(step)
    return children


def downstream_closure(
    step_ids: list[str], dependencies: dict[str, set[str]], failed: str
) -> list[str]:
    """failed 的所有下游（含间接），按输入顺序稳定，不含自身。"""
    children = dependents_map(step_ids, dependencies)
    seen: set[str] = set()
    queue = list(children.get(failed, []))
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        queue.extend(children.get(current, []))
    return [step for step in step_ids if step in seen]
