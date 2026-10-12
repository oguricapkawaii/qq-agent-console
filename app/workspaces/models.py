"""目录查找、绑定与选择的模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DirectoryCandidate:
    """一个候选目录（只描述，不读取内容）。"""

    path: Path
    label: str
    score: int
    reason: str

    def render(self, index: int) -> str:
        return f"{index}. {self.path}"


@dataclass(frozen=True, slots=True)
class Resolution:
    """目录选择器解析结果。"""

    workspace: Path | None = None
    source: str = "none"  # explicit | binding | none
    selection_code: str = ""
    candidates: tuple[DirectoryCandidate, ...] = ()
    message: str = ""

    @property
    def awaiting_selection(self) -> bool:
        return bool(self.selection_code)


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    """一次 `/workspace select` 的结果。"""

    ok: bool
    workspace: Path | None = None
    message: str = ""
    entry: str = ""
    # 选择完成后需要续跑的原任务（单任务入口）
    resume_task_id: str = ""
    resume_payload: dict | None = None
    # 团队步骤草稿的定位信息
    team_id: str = ""
    step_id: str = ""
    draft_version: int = 0


class WorkspaceError(ValueError):
    """目录服务错误；message 可直接回执。"""


class TaskCancelled(RuntimeError):
    """任务在排队/执行期间被取消：工具层必须立刻停止后续动作（F05）。

    放在这里是为了让 workspaces / backends / tasks 三层共用同一个信号，
    而不产生互相导入。
    """
