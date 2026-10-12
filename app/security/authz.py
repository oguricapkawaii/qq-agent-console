"""固定管理员和按群持久化的成员白名单；默认拒绝。"""
from __future__ import annotations
from collections.abc import Iterable
from app.storage import ContextStore


class Authorizer:
    def __init__(self, allowed_user_ids: Iterable[int], allowed_group_ids: Iterable[int]) -> None:
        # 只有部署配置中的既有用户可以管理授权。动态成员永远不能提升为管理员。
        self._users = frozenset(allowed_user_ids)
        self._groups = frozenset(allowed_group_ids)
        self.store: ContextStore | None = None

    def admin_allowed(self, user_id: int) -> bool:
        return user_id in self._users

    def user_allowed(self, user_id: int, group_id: int | None = None) -> bool:
        if group_id is not None and not self.group_allowed(group_id):
            return False
        return user_id in self.members(group_id)

    def members(self, group_id: int | None) -> set[int]:
        users = set(self._users)
        if self.store is not None and group_id is not None:
            for user, allowed in self.store.whitelist(group_id).items():
                if allowed:
                    users.add(user)
                elif user not in self._users:
                    users.discard(user)
        return users

    def group_allowed(self, group_id: int) -> bool:
        return group_id in self._groups
