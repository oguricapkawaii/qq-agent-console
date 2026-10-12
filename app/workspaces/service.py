"""WorkspaceService：目录授权解析、绑定、模糊查找与候选确认。

职责（统一架构第 4 节）：
- 把用户的 `--dir` / `--find` / `--dir .` 解析成**规范绝对路径**，绝不回落到进程 cwd；
- 授权判定交给 AclService（用户 ACL ∩ 步骤声明 ∩ 后端能力 ∩ 全局策略）；
- find 只在授权搜索根内做有上限搜索，返回候选并等待用户 select；
- 绑定按「群 × 发起人」隔离并持久化，使用前复查授权；
- 选择确认绑定群、发起人、请求、草稿版本、候选快照与 TTL。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
import os
from pathlib import Path

from app.commands.options import PublicOptions
from app.security.acl import AclService
from app.security.confirmations import (
    ConfirmationKind,
    ConfirmationOutcome,
    InMemoryConfirmationStore,
)
from app.security.paths import PathBoundaryError, canonical, is_within
from app.workspaces.coordinator import WorkspaceCoordinator
from app.workspaces.finder import DEFAULT_EXCLUDES, SearchBudget, find_directories
from app.workspaces.models import (
    DirectoryCandidate,
    Resolution,
    SelectionOutcome,
    WorkspaceError,
)

logger = logging.getLogger("qqbot.workspaces.service")


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    max_depth: int = 4
    max_candidates: int = 8
    max_entries: int = 20000
    max_seconds: float = 2.0
    bind_ttl_seconds: float = 300.0

    def budget(self) -> SearchBudget:
        return SearchBudget(
            max_depth=self.max_depth, max_candidates=self.max_candidates,
            max_entries=self.max_entries, max_seconds=self.max_seconds,
        )


class WorkspaceService:
    def __init__(
        self,
        *,
        acl: AclService,
        store,
        confirmations: InMemoryConfirmationStore,
        coordinator: WorkspaceCoordinator,
        limits: WorkspaceLimits | None = None,
        excludes: frozenset[str] = DEFAULT_EXCLUDES,
    ) -> None:
        self._acl = acl
        self._store = store
        self._confirmations = confirmations
        self._coordinator = coordinator
        self._limits = limits or WorkspaceLimits()
        self._excludes = excludes

    # ---------- 绑定 ----------

    def binding(self, *, group_id: int, user_id: int) -> Path | None:
        """返回当前绑定目录；使用前复查授权，撤销后视为无绑定。"""
        raw = self._store.get_binding(group_id=group_id, user_id=user_id)
        if not raw:
            return None
        try:
            path = canonical(raw)
        except PathBoundaryError:
            return None
        if not path.is_dir():
            return None
        decision = self._acl.authorize_path(
            user_id=user_id, group_id=group_id, path=path, mode="read"
        )
        if not decision.allowed:
            logger.info(
                "绑定目录授权已失效 group=%d user=%d reason=%s",
                group_id, user_id, decision.reason,
            )
            return None
        return path

    def bind(self, *, group_id: int, user_id: int, raw_path: str) -> tuple[Path, str]:
        try:
            path = canonical(raw_path)
        except PathBoundaryError as exc:
            raise WorkspaceError(str(exc)) from None
        if not path.is_dir():
            raise WorkspaceError(f"目录不存在：{path}")
        decision = self._acl.authorize_path(
            user_id=user_id, group_id=group_id, path=path, mode="read"
        )
        if not decision.allowed:
            raise WorkspaceError(decision.reason)
        self._store.set_binding(group_id=group_id, user_id=user_id, path=str(path))
        modes = "/".join(sorted(decision.granted_modes)) or "read"
        return path, (
            f"📌 已绑定目录（本群 × 你）：{path}\n"
            f"该目录上你的权限：{modes}\n"
            f"后续可用 /task <agent> --dir . --mode read|edit|exec -- <任务>；"
            f"绑定不会给纯文本任务加权限。"
        )

    def clear(self, *, group_id: int, user_id: int) -> str:
        existed = self._store.clear_binding(group_id=group_id, user_id=user_id)
        return "🧹 已清除本群中你的目录绑定。" if existed else "你在这个群没有绑定目录。"

    def status_text(self, *, group_id: int, user_id: int) -> str:
        bound = self.binding(group_id=group_id, user_id=user_id)
        lines = ["📂 你的目录状态（本群 × 你）："]
        if bound is None:
            raw = self._store.get_binding(group_id=group_id, user_id=user_id)
            lines.append(f"当前绑定：{raw}（授权已失效或不复存在）" if raw else "当前绑定：（无）")
        else:
            decision = self._acl.authorize_path(
                user_id=user_id, group_id=group_id, path=bound, mode="read"
            )
            modes = "/".join(sorted(decision.granted_modes)) or "read"
            lines.append(f"当前绑定：{bound}")
            lines.append(f"你的权限：{modes}")
        grants = self._acl.search_grants()
        if grants:
            lines.append("授权搜索根（find 的范围）：")
            for grant in grants:
                lines.append(f"  · {grant.label or grant.path}")
        else:
            lines.append("授权搜索根：未配置（find 不可用）")
        lines.append("用法：/workspace find <关键词>、/workspace use \"绝对路径\"、/workspace clear")
        return "\n".join(lines)

    # ---------- 查找 ----------

    def find_candidates(
        self, *, group_id: int, user_id: int, keyword: str
    ) -> tuple[tuple[DirectoryCandidate, ...], str]:
        """在授权搜索根内查找候选；返回（候选, 提示文案）。"""
        grants = [
            grant for grant in self._acl.search_grants()
            if self._acl.authorize_path(
                user_id=user_id, group_id=group_id, path=grant.path, mode="read"
            ).allowed
        ]
        if not grants:
            raise WorkspaceError(
                "没有可搜索的授权根：请让管理员配置 workspaces.roots（含 read 权限、searchable），"
                "或先用 /workspace use 指定已知路径。"
            )
        report = find_directories(
            keyword, tuple(grant.path for grant in grants),
            budget=self._limits.budget(), excludes=self._excludes,
        )
        if not report.candidates:
            hint = "（搜索已达时间/数量上限，可缩小关键词）" if report.truncated else ""
            return (), (
                f"未在授权范围内找到与「{keyword}」匹配的目录{hint}。\n"
                f"扫描 {report.scanned_entries} 个条目，用时 {report.elapsed_ms}ms。\n"
                "可以换个关键词，或用 /workspace use \"绝对路径\" 直接绑定已知目录。"
            )
        return report.candidates, ""

    # ---------- 解析选择器 ----------

    def resolve_selector(
        self, *, group_id: int, user_id: int, options: PublicOptions, mode: str,
        task_id: str = "", draft_version: int = 0, resume_payload: dict | None = None,
        purpose: str = "task",
    ) -> Resolution:
        """把 --dir/--find/绑定目录解析为规范路径，或返回待确认的候选列表。"""
        if options.find is not None:
            candidates, message = self.find_candidates(
                group_id=group_id, user_id=user_id, keyword=options.find
            )
            if not candidates:
                return Resolution(workspace=None, source="none", message=message)
            return self._propose_selection(
                group_id=group_id, user_id=user_id, candidates=candidates, mode=mode,
                task_id=task_id, draft_version=draft_version, resume_payload=resume_payload,
                purpose=purpose,
            )
        if options.directory is not None:
            if options.directory.strip() == ".":
                bound = self.binding(group_id=group_id, user_id=user_id)
                if bound is None:
                    return Resolution(
                        workspace=None, source="none",
                        message="--dir . 需要先绑定目录：请用 /workspace find 或 /workspace use 绑定；"
                                "没有绑定时不会回落到进程工作目录。",
                    )
                decision = self._acl.authorize_path(
                    user_id=user_id, group_id=group_id, path=bound, mode=mode
                )
                if not decision.allowed:
                    return Resolution(workspace=None, source="none", message=decision.reason)
                return Resolution(workspace=bound, source="binding")
            try:
                raw = options.directory.strip()
                if not Path(raw).is_absolute() and not raw.startswith("~"):
                    # 相对路径按"本群×你"的绑定目录解析（没有绑定则明确拒绝）
                    base = self.binding(group_id=group_id, user_id=user_id)
                    if base is None:
                        return Resolution(
                            workspace=None, source="none",
                            message=(
                                f"--dir {raw} 是相对路径，需要先绑定目录；"
                                "请用 /workspace use 或 /workspace find 绑定，或改用绝对路径。"
                            ),
                        )
                    path = canonical(raw, base=base)
                else:
                    path = canonical(raw)
            except PathBoundaryError as exc:
                return Resolution(workspace=None, source="none", message=str(exc))
            if not path.is_dir():
                return Resolution(
                    workspace=None, source="none",
                    message=f"目录不存在或不是目录：{path}（如需模糊查找请用 --find \"关键词\"）",
                )
            decision = self._acl.authorize_path(
                user_id=user_id, group_id=group_id, path=path, mode=mode
            )
            if not decision.allowed:
                return Resolution(workspace=None, source="none", message=decision.reason)
            return Resolution(workspace=path, source="explicit")
        # 没有目录选择器：显式文件模式用绑定目录
        if mode in {"read", "edit", "exec"}:
            bound = self.binding(group_id=group_id, user_id=user_id)
            if bound is None:
                return Resolution(
                    workspace=None, source="none",
                    message=f"--mode {mode} 需要目录：请加 --dir \"路径\"、--find \"关键词\"，"
                            "或先用 /workspace 绑定目录（没有绑定时不会回落到进程工作目录）。",
                )
            decision = self._acl.authorize_path(
                user_id=user_id, group_id=group_id, path=bound, mode=mode
            )
            if not decision.allowed:
                return Resolution(workspace=None, source="none", message=decision.reason)
            return Resolution(workspace=bound, source="binding")
        return Resolution(workspace=None, source="none")

    def _propose_selection(
        self, *, group_id: int, user_id: int, candidates: tuple[DirectoryCandidate, ...],
        mode: str, task_id: str, draft_version: int, resume_payload: dict | None,
        purpose: str,
    ) -> Resolution:
        payload = {
            "candidates": [
                {
                    "path": str(item.path), "label": item.label, "score": item.score,
                    # F13：冻结可验证的目录身份，避免同路径被替换后仍被确认
                    "identity": _directory_identity(item.path),
                }
                for item in candidates
            ],
            "mode": mode,
            "purpose": purpose,
            "resume": resume_payload or {},
        }
        record = self._confirmations.create(
            kind=ConfirmationKind.WORKSPACE_SELECT,
            user_id=user_id,
            group_id=group_id,
            task_id=task_id,
            draft_version=draft_version,
            summary=f"选择目录（{len(candidates)} 个候选，用途：{purpose}）",
            payload=payload,
            ttl_seconds=self._limits.bind_ttl_seconds,
        )
        lines = [f"🔍 找到 {len(candidates)} 个候选目录："]
        for index, item in enumerate(candidates, start=1):
            lines.append(item.render(index))
        lines.append(
            f"请发送：/workspace select {record.code} <序号>"
            f"（{int(self._limits.bind_ttl_seconds)} 秒内有效，仅你本人可用）"
        )
        lines.append("只有一个候选也需要选择；过期后请重新查找。")
        return Resolution(
            workspace=None, source="selection", selection_code=record.code,
            candidates=candidates, message="\n".join(lines),
        )

    # ---------- 选择 ----------

    def select(
        self, *, group_id: int, user_id: int, code: str, index: int
    ) -> SelectionOutcome:
        outcome, record = self._confirmations.consume_record(
            kind=ConfirmationKind.WORKSPACE_SELECT, user_id=user_id, code=code,
            group_id=group_id,
        )
        if outcome is not ConfirmationOutcome.OK or record is None:
            return SelectionOutcome(ok=False, message=self._selection_error(outcome))
        candidates = record.payload.get("candidates") or []
        if not isinstance(candidates, list) or not 1 <= index <= len(candidates):
            return SelectionOutcome(
                ok=False,
                message=f"序号超出范围：该确认码只有 {len(candidates)} 个候选。请重新查找。",
            )
        raw = str(candidates[index - 1].get("path") or "")
        try:
            path = canonical(raw)
        except PathBoundaryError as exc:
            return SelectionOutcome(ok=False, message=f"候选路径已不可用：{exc}")
        if not path.is_dir():
            return SelectionOutcome(
                ok=False, message=f"候选目录已不存在：{path}；请重新查找。"
            )
        expected_identity = candidates[index - 1].get("identity")
        current_identity = _directory_identity(path)
        if expected_identity and current_identity != expected_identity:
            return SelectionOutcome(
                ok=False,
                message=(
                    "候选目录在确认前已被替换（目录身份不一致），已拒绝；"
                    "请重新 /workspace find 选择。"
                ),
            )
        mode = str(record.payload.get("mode") or "read")
        decision = self._acl.authorize_path(
            user_id=user_id, group_id=group_id, path=path, mode=mode
        )
        if not decision.allowed:
            return SelectionOutcome(ok=False, message=decision.reason)
        purpose = str(record.payload.get("purpose") or "")
        if purpose != "team":
            # team 步骤只把路径冻结进计划，不改变用户的绑定目录
            self._store.set_binding(group_id=group_id, user_id=user_id, path=str(path))
        resume = record.payload.get("resume") or {}
        return SelectionOutcome(
            ok=True,
            workspace=path,
            message=f"✅ 已选择并绑定目录：{path}",
            entry=purpose,
            resume_task_id=str(resume.get("task_id") or ""),
            resume_payload=resume or None,
            team_id=str(resume.get("team_id") or ""),
            step_id=str(resume.get("step_id") or ""),
            draft_version=int(record.draft_version or 0),
        )

    @staticmethod
    def _selection_error(outcome: ConfirmationOutcome) -> str:
        return {
            ConfirmationOutcome.NOT_FOUND: "确认码无效或已被使用；请重新 /workspace find。",
            ConfirmationOutcome.EXPIRED: "确认码已过期；请重新 /workspace find。",
            ConfirmationOutcome.WRONG_USER: "该确认码不属于你。",
            ConfirmationOutcome.WRONG_SCOPE: "该确认码是在别的群发起的，不能在当前群使用。",
            ConfirmationOutcome.WRONG_KIND: "确认码类型不匹配。",
        }.get(outcome, "确认失败。")

    # ---------- 工具 ----------

    def invalidate_for_draft(self, team_id: str, draft_version: int) -> int:
        """草稿变化时使**旧版本**的确认失效（当前版本的确认保留）。"""
        return self._confirmations.invalidate(
            lambda record: (
                record.payload.get("resume", {}).get("team_id") == team_id
                and record.draft_version < draft_version
            )
        )

    def is_locked(self, path: Path) -> bool:
        return bool(self._coordinator.conflicts((path,)))

    def path_within_root(self, path: Path) -> bool:
        grant = self._acl.find_grant(path)
        return grant is not None and is_within(path, grant.path)


def _directory_identity(path: Path) -> dict | None:
    """目录身份：真实路径 + 设备号 + inode（被替换/重建即可检出）。"""
    try:
        stat = os.stat(str(path), follow_symlinks=True)
    except OSError:
        return None
    if not os.path.isdir(str(path)):
        return None
    return {
        "realpath": os.path.realpath(str(path)),
        "dev": int(stat.st_dev),
        "ino": int(stat.st_ino),
    }
