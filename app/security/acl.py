"""统一 ACL：模式权限、目录授权与全局策略的交集。

有效能力 = 用户目录授权 ∩ 步骤声明 ∩ 后端真实能力 ∩ 全局策略。

- 未授权用户：仅 text（并被调度器限制为 /help）；
- 授权成员：text 默认可用；read/edit/exec 需要显式目录授权；
- 管理员：授权根内 read/edit，exec 需全局开启且目录授权包含 exec；
- 实时撤权：每次工具/文件动作前重新计算，不缓存判定结果。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT, MODES
from app.security.authz import Authorizer
from app.security.paths import PathBoundaryError, canonical, is_protected, is_within

MODE_RANK: dict[str, int] = {MODE_TEXT: 0, MODE_READ: 1, MODE_EDIT: 2, MODE_EXEC: 3}


def mode_allows(granted: str, requested: str) -> bool:
    """granted 是否覆盖 requested（exec ⊃ edit ⊃ read ⊃ text）。"""
    return MODE_RANK.get(granted, -1) >= MODE_RANK.get(requested, 99)


def normalize_modes(values: Iterable[str]) -> frozenset[str]:
    modes = {str(value).strip().lower() for value in values if str(value).strip()}
    unknown = modes - set(MODES)
    if unknown:
        raise ValueError(f"未知权限模式：{', '.join(sorted(unknown))}（可用 text/read/edit/exec）")
    return frozenset(modes)


@dataclass(frozen=True, slots=True)
class RootGrant:
    """一个授权根的配置。"""

    path: Path
    modes: frozenset[str] = frozenset({MODE_READ})
    admin_modes: frozenset[str] = frozenset()
    users: Mapping[int, frozenset[str]] = field(default_factory=dict)
    searchable: bool = True
    label: str = ""

    def user_modes(self, user_id: int, *, admin: bool) -> frozenset[str]:
        if user_id in self.users:
            return frozenset(self.users[user_id])
        return self.admin_modes if admin else self.modes


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    mode: str = MODE_TEXT
    reason: str = ""
    grant: RootGrant | None = None
    granted_modes: frozenset[str] = frozenset()
    path: Path | None = None


class AclService:
    """目录 ACL 与全局策略的统一判定服务。"""

    def __init__(
        self,
        *,
        authz: Authorizer,
        grants: Sequence[RootGrant] = (),
        protected_roots: Sequence[Path] = (),
        exec_enabled: bool = True,
        edit_enabled: bool = True,
    ) -> None:
        self._authz = authz
        self._grants = tuple(grants)
        self._protected = tuple(protected_roots)
        self._exec_enabled = exec_enabled
        self._edit_enabled = edit_enabled

    # ---------- 基础查询 ----------

    @property
    def grants(self) -> tuple[RootGrant, ...]:
        return self._grants

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        """受保护范围（凭据/机器人自身数据），broker 与 exec 共用。"""
        return self._protected

    def describe_grants(self) -> list[dict]:
        """只输出可用于帮助/状态描述的公开信息，不泄露真实路径以外的内容。"""
        rows = []
        for grant in self._grants:
            rows.append({
                "label": grant.label or str(grant.path),
                "modes": sorted(grant.modes),
                "admin_modes": sorted(grant.admin_modes),
                "searchable": grant.searchable,
                "user_overrides": sorted(int(user) for user in grant.users),
            })
        return rows

    def search_grants(self) -> tuple[RootGrant, ...]:
        return tuple(grant for grant in self._grants if grant.searchable)

    def find_grant(self, path: Path) -> RootGrant | None:
        """返回包含 path 的授权根；多个命中时取最具体（最长）的那个。"""
        try:
            path = canonical(str(path))
        except PathBoundaryError:
            return None
        matches = [grant for grant in self._grants if is_within(path, grant.path)]
        if not matches:
            return None
        return max(matches, key=lambda grant: len(grant.path.parts))

    # ---------- 判定 ----------

    def user_modes_for_path(self, *, user_id: int, group_id: int, path: Path) -> frozenset[str]:
        if not self._authz.group_allowed(group_id):
            return frozenset()
        if not self._authz.user_allowed(user_id, group_id):
            return frozenset()
        grant = self.find_grant(path)
        if grant is None:
            return frozenset()
        return grant.user_modes(user_id, admin=self._authz.admin_allowed(user_id))

    def effective_modes_for_path(self, *, user_id: int, path: Path) -> frozenset[str]:
        """返回该用户在某路径上**实际生效**的模式（最具体授权 + users 覆盖 + admin 回落）。

        与 `authorize_path` 使用同一套解析：先找最具体的授权根（`find_grant`），再按
        `users[user_id]` → `admin_modes` → `modes` 求值。exec 沙箱限制必须用这个结果，
        不能用 `grant.modes`/`grant.admin_modes` 代替——后者会把别人的权限算成当前用户的
        （T01：父目录 exec、子目录 `users[100]=text` 时，公共/admin 集合仍然含 read/edit）。
        """
        grant = self.find_grant(path)
        if grant is None:
            return frozenset()
        return grant.user_modes(user_id, admin=self._authz.admin_allowed(user_id))

    def authorize_path(
        self, *, user_id: int, group_id: int, path: Path, mode: str,
        step_modes: frozenset[str] | None = None, backend_modes: frozenset[str] | None = None,
    ) -> AccessDecision:
        """目录路径上的模式判定（text 不需要路径，直接通过）。

        入参路径一律先规范化（真实路径 + 父链），因此调用方即使传入
        `/var/...` 与 `/private/var/...` 这类别名，判定结果也一致。
        """
        if mode not in MODE_RANK:
            return AccessDecision(False, mode, f"未知模式：{mode}")
        try:
            path = canonical(str(path))
        except PathBoundaryError as exc:
            return AccessDecision(False, mode, str(exc))
        if not self._authz.group_allowed(group_id):
            return AccessDecision(False, mode, "该群未授权使用本机器人。")
        if not self._authz.user_allowed(user_id, group_id):
            return AccessDecision(False, mode, "你的授权已被撤销或尚未授权。")
        if is_protected(path, self._protected):
            return AccessDecision(False, mode, "目标位于受保护范围（凭据/机器人自身数据）。")
        grant = self.find_grant(path)
        if grant is None:
            return AccessDecision(
                False, mode,
                "该目录不在授权根内；请让管理员配置 workspaces.roots 或先用 /workspace 绑定授权目录。",
            )
        granted = grant.user_modes(user_id, admin=self._authz.admin_allowed(user_id))
        if not granted:
            return AccessDecision(False, mode, "你在该目录没有授权。", grant=grant, path=path)
        if not mode_allows(max(granted, key=lambda item: MODE_RANK[item]), mode):
            return AccessDecision(
                False, mode,
                f"你在该目录只有 {'/'.join(sorted(granted))} 权限，不能执行 {mode}；"
                "如需提升请联系管理员调整目录 ACL。",
                grant=grant, granted_modes=granted, path=path,
            )
        if mode == MODE_EXEC and not self._exec_enabled:
            return AccessDecision(False, mode, "全局策略已关闭 exec 模式。", grant=grant, path=path)
        if mode == MODE_EDIT and not self._edit_enabled:
            return AccessDecision(False, mode, "全局策略已关闭 edit 模式。", grant=grant, path=path)
        if step_modes is not None and mode not in step_modes and not mode_allows(
            max(step_modes, key=lambda item: MODE_RANK[item]) if step_modes else MODE_TEXT, mode
        ):
            return AccessDecision(False, mode, "本步骤的权限声明不含该模式。", grant=grant, path=path)
        if backend_modes is not None and mode not in backend_modes:
            return AccessDecision(
                False, mode,
                f"该后端不支持 {mode} 模式（capability_not_supported）。",
                grant=grant, path=path,
            )
        return AccessDecision(
            True, mode, "", grant=grant, granted_modes=granted, path=path
        )

    def authorize_binding(
        self, *, user_id: int, group_id: int, mode: str,
    ) -> AccessDecision:
        """校验"可以使用绑定目录"本身（路径由 WorkspaceService 解析）。"""
        if not self._authz.group_allowed(group_id):
            return AccessDecision(False, mode, "该群未授权使用本机器人。")
        if not self._authz.user_allowed(user_id, group_id):
            return AccessDecision(False, mode, "你的授权已被撤销或尚未授权。")
        return AccessDecision(True, mode)

    def resolve_mode(
        self, *, user_id: int, group_id: int, requested: str | None,
        has_selector: bool, mode_override_allowed: bool = True,
    ) -> str:
        """按规格推导并校验模式（不从正文推断）。"""
        if requested is not None:
            return requested
        return MODE_READ if has_selector else MODE_TEXT

    # ---------- 绑定目录的根判定 ----------

    def binding_allowed(self, *, user_id: int, group_id: int, path: Path) -> AccessDecision:
        """绑定目录时必须至少具备 read 能力。"""
        decision = self.authorize_path(
            user_id=user_id, group_id=group_id, path=path, mode=MODE_READ
        )
        return decision

    def canonical_or_error(self, raw: str) -> Path:
        try:
            return canonical(raw)
        except PathBoundaryError as exc:
            raise
