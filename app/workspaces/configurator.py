"""目录配置解析：新式 `workspaces:` 结构与旧式 per-agent 目录字段的迁移。

迁移原则（统一架构第 4 节 / 验收 A21）：
- 旧 `/read` 是**管理员命令**：FILE_READ_ROOT 迁移为成员**无权限**、管理员 read
  （F09：迁移不得把管理员专属能力静默扩大给普通成员，也不得把只读根升级为可写）；
- FILE_WRITE_ROOT 过去只允许管理员写：成员仍为不可读写，管理员 read/edit；
- CODEX_ALLOWED_ROOTS 过去是管理员 read/edit/exec：迁移为 admin_modes=read/edit/exec，
  成员默认无授权；
- 新旧同时存在时**不静默选择**：新配置生效，并逐条列出被忽略的旧字段与实际授权，
  启动日志与 /status 都会展示。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ
from app.config import Settings
from app.security.acl import RootGrant, normalize_modes
from app.security.paths import PathBoundaryError, canonical
from app.workspaces.coordinator import enforce_worker_policy
from app.workspaces.finder import DEFAULT_EXCLUDES
from app.workspaces.service import WorkspaceLimits


class WorkspaceConfigError(ValueError):
    """目录配置非法。"""


@dataclass(slots=True)
class WorkspaceConfig:
    grants: tuple[RootGrant, ...] = ()
    protected: tuple[Path, ...] = ()
    limits: WorkspaceLimits = field(default_factory=WorkspaceLimits)
    excludes: frozenset[str] = DEFAULT_EXCLUDES
    exec_enabled: bool = True
    edit_enabled: bool = True
    max_workers: int = 1
    cross_process_lock: bool = False
    lock_timeout: float = 30.0
    # 覆盖前备份的受控目录（F04：不再写目标同目录 .bak）
    backup_dir: Path | None = None
    notes: tuple[str, ...] = ()


def project_root() -> Path:
    """机器人自身项目目录（受保护范围）。"""
    return Path(__file__).resolve().parents[2]


def default_protected() -> tuple[Path, ...]:
    root = project_root()
    return (root, root / "data", root / "runtime", root / ".env", root / "backups")


def build_workspace_config(settings: Settings) -> WorkspaceConfig:
    raw = settings.console_workspaces or {}
    if not isinstance(raw, dict):
        raise WorkspaceConfigError("workspaces: 必须是映射")
    notes: list[str] = []
    exec_enabled = bool(raw.get("exec_enabled", True))
    edit_enabled = bool(raw.get("edit_enabled", True))
    limits = _limits(raw.get("find") or {})
    excludes = frozenset(str(item) for item in raw.get("exclude_dirs") or ()) or DEFAULT_EXCLUDES

    protected = list(default_protected())
    for item in raw.get("protected_paths") or ():
        try:
            protected.append(canonical(str(item)))
        except PathBoundaryError as exc:
            raise WorkspaceConfigError(f"protected_paths 非法：{exc}") from None

    configured_roots = raw.get("roots")
    grants: list[RootGrant] = []
    if configured_roots:
        if not isinstance(configured_roots, list):
            raise WorkspaceConfigError("workspaces.roots 必须是列表")
        for index, item in enumerate(configured_roots):
            grants.append(_grant_from_config(item, index))
        legacy = _legacy_grants(settings)
        if legacy:
            notes.append(
                "检测到新式 workspaces.roots 与旧式目录字段同时存在：已按 workspaces.roots 生效，"
                "以下旧字段被忽略（请迁移后删除）。"
            )
            notes.extend(_describe_legacy(legacy))
    else:
        grants = _legacy_grants(settings)
        if grants:
            notes.append(
                "未配置 workspaces.roots：已按保守规则迁移旧式目录字段，逐条授权如下。"
            )
            notes.extend(_describe_legacy(grants))

    max_workers = int(raw.get("max_workers") or settings.workspace_max_workers or 1)
    cross_process = bool(
        raw.get("cross_process_lock", settings.workspace_cross_process_lock)
    )
    enforce_worker_policy(
        max_workers=max_workers, cross_process_lock=cross_process, source="workspaces"
    )
    lock_timeout = float(
        raw.get("lock_timeout_seconds") or settings.workspace_lock_timeout_seconds
    )
    backup_dir = Path(
        str(raw.get("backup_dir") or (project_root() / "runtime/state/backups"))
    ).expanduser()
    try:
        backup_dir = Path(os.path.realpath(str(backup_dir)))
    except OSError:  # pragma: no cover - 极端平台
        pass
    return WorkspaceConfig(
        grants=tuple(grants),
        protected=tuple(protected),
        limits=limits,
        excludes=excludes,
        exec_enabled=exec_enabled,
        edit_enabled=edit_enabled,
        max_workers=max_workers,
        cross_process_lock=cross_process,
        lock_timeout=lock_timeout,
        backup_dir=backup_dir,
        notes=tuple(notes),
    )


def _limits(raw: Any) -> WorkspaceLimits:
    if not isinstance(raw, dict):
        raw = {}
    return WorkspaceLimits(
        max_depth=int(raw.get("max_depth") or 4),
        max_candidates=int(raw.get("max_candidates") or 8),
        max_entries=int(raw.get("max_entries") or 20000),
        max_seconds=float(raw.get("max_seconds") or 2.0),
        bind_ttl_seconds=float(raw.get("selection_ttl_seconds") or 300.0),
    )


def _grant_from_config(item: Any, index: int) -> RootGrant:
    if not isinstance(item, dict):
        raise WorkspaceConfigError(f"workspaces.roots[{index}] 必须是映射")
    path_raw = item.get("path")
    if not path_raw:
        raise WorkspaceConfigError(f"workspaces.roots[{index}] 缺少 path")
    try:
        path = canonical(str(path_raw))
    except PathBoundaryError as exc:
        raise WorkspaceConfigError(f"workspaces.roots[{index}].path 非法：{exc}") from None
    if not path.is_dir():
        raise WorkspaceConfigError(f"workspaces.roots[{index}].path 不是已存在目录：{path}")
    users: dict[int, frozenset[str]] = {}
    for user, modes in (item.get("users") or {}).items():
        try:
            users[int(user)] = normalize_modes(modes)
        except (TypeError, ValueError) as exc:
            raise WorkspaceConfigError(
                f"workspaces.roots[{index}].users[{user}] 非法：{exc}"
            ) from None
    try:
        modes = normalize_modes(item.get("modes") or [MODE_READ])
        admin_modes = normalize_modes(item.get("admin_modes") or [MODE_READ, MODE_EDIT])
    except ValueError as exc:
        raise WorkspaceConfigError(f"workspaces.roots[{index}] 权限声明非法：{exc}") from None
    return RootGrant(
        path=path,
        modes=modes,
        admin_modes=admin_modes,
        users=users,
        searchable=bool(item.get("searchable", True)),
        label=str(item.get("label") or path.name),
    )


def _legacy_grants(settings: Settings) -> list[RootGrant]:
    grants: list[RootGrant] = []

    def add(raw: str, *, modes, admin_modes, label: str, searchable: bool = True) -> None:
        text = (raw or "").strip()
        if not text:
            return
        try:
            path = canonical(text)
        except PathBoundaryError:
            return
        if not path.is_dir():
            return
        grants.append(RootGrant(
            path=path, modes=frozenset(modes), admin_modes=frozenset(admin_modes),
            users={}, searchable=searchable, label=label,
        ))

    # 旧 /read 是管理员命令：迁移后成员 modes 为空，只有管理员可读。
    # F09：不得把管理员专属能力静默扩大给普通成员。
    add(settings.file_read_root, modes=set(), admin_modes={MODE_READ},
        label="FILE_READ_ROOT（旧管理员只读命令：成员无权限，管理员 read）")
    add(settings.file_write_root, modes=set(), admin_modes={MODE_READ, MODE_EDIT},
        label="FILE_WRITE_ROOT（旧写根：成员仍无权限，管理员 read/edit）")
    for root in settings.codex_allowed_roots:
        add(root, modes=set(), admin_modes={MODE_READ, MODE_EDIT, MODE_EXEC},
            label="CODEX_ALLOWED_ROOTS（管理员 read/edit/exec）")
    add(settings.zcode_workspace_root, modes=set(), admin_modes={MODE_READ, MODE_EDIT},
        label="ZCODE_WORKSPACE_ROOT（管理员 read/edit）")
    return grants


def _describe_legacy(grants: list[RootGrant]) -> list[str]:
    lines = []
    for grant in grants:
        modes = "/".join(sorted(grant.modes)) or "无"
        admin = "/".join(sorted(grant.admin_modes)) or "无"
        lines.append(f"  · {grant.label} → {grant.path}：成员 {modes}，管理员 {admin}")
    lines.append(
        "  迁移不会把原本只读的根变成可写根；如需更宽权限请在 workspaces.roots 显式声明。"
    )
    return lines
