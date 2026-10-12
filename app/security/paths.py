"""路径规范化与边界判定。

安全要求（统一架构第 4 节）：
- 绝不用字符串 `startswith` 判定目录范围；
- 规范化必须处理中文、空格、符号链接、根路径、相邻同前缀目录、父子目录；
- 判定基于"真实路径（realpath）+ 父链"，并对不存在的路径做最接近存在祖先的解析，
  使 `a/../b`、`x/../../etc` 之类在解析阶段就被拒绝。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 敏感文件名/目录名：默认不向模型提供
SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", "auth.json", "secrets.yaml", "secrets.yml",
    ".netrc", ".pgpass", ".git-credentials", "session.db",
})
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".codex", ".zcode", ".config", ".git"})


class PathBoundaryError(ValueError):
    """路径越界或无法安全解析。message 可直接回执。"""


def canonical(raw: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """把不可信路径解析为真实绝对路径。

    - 相对路径必须给出 base；没有 base 时拒绝（绝不回落到进程 cwd）；
    - 不存在的路径：解析到最接近的存在祖先，再把剩余部分拼回去，
      这样 `root/newdir/file` 也能得到规范化结果，同时符号链接无法逃逸。
    """
    text = str(raw or "").strip()
    if not text:
        raise PathBoundaryError("路径为空。")
    if text.startswith("~"):
        raise PathBoundaryError("不支持 ~ 展开，请写绝对路径或已绑定目录内的相对路径。")
    candidate = Path(text)
    if not candidate.is_absolute():
        if base is None:
            raise PathBoundaryError("只允许绝对路径，或已绑定目录内的相对路径。")
        candidate = base / candidate
    try:
        return _realpath_with_missing(candidate)
    except OSError:
        raise PathBoundaryError("路径无法解析（权限或 IO 错误）。") from None


def _realpath_with_missing(path: Path) -> Path:
    """解析真实路径；缺失的尾部逐级保留，但祖先一律先真实化。"""
    path = Path(os.path.normpath(str(path)))
    missing: list[str] = []
    probe = path
    while True:
        if probe.exists() or probe.is_symlink():
            resolved = Path(os.path.realpath(str(probe)))
            return resolved.joinpath(*reversed(missing)) if missing else resolved
        parent = probe.parent
        if parent == probe:  # 到达根
            return Path(os.path.realpath(str(path)))
        missing.append(probe.name)
        probe = parent


def is_within(path: Path, root: Path) -> bool:
    """path 是否等于 root 或位于 root 之内（两者都应为规范路径）。"""
    if path == root:
        return True
    return root in path.parents


def overlap(left: Path, right: Path) -> bool:
    """两个目录是否相同或存在父子包含关系（用于目录锁冲突判定）。"""
    return is_within(left, right) or is_within(right, left)


def is_sensitive(path: Path) -> bool:
    """是否命中默认敏感清单（凭据/密钥/数据库等）。"""
    if path.name in SENSITIVE_NAMES or path.name.startswith(".env"):
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    for part in path.parts:
        if part in SENSITIVE_DIRS:
            return True
    return False


# 与 `is_sensitive` 语义等价的 SEATBELT 正则清单（沙箱 profile 用）。
#
# 重要实现约束：`sandbox-exec`（SBPL）的 regex 支持是**部分**的——把多个分支写进一个
# `(a|b|c)` 组会静默失配（实测 `#"/[^/]*(\.pem|\.key)$"` 不生效，而 `#"[.]pem$"` 生效）。
# 因此这里为每个敏感形态生成**独立的 deny 规则**，只用最朴素的正则特性：
# 字面量、`.`、`\.`、`$`。任何新增敏感名都必须同时出现在两处，否则会出现
# "broker 拒绝、exec 却能读"的边界缝隙。
def seatbelt_patterns() -> tuple[str, ...]:
    """返回沙箱 profile 使用的敏感路径正则（与 `is_sensitive` 共用同一份清单）。

    - 文件名：`.env*` 前缀、精确凭据名、密钥后缀，各自一条规则；
    - 目录名：`.ssh`、`.aws`、`.git` 等，匹配其自身与其下内容。
    """
    patterns: list[str] = [r"/\.env"]
    for name in sorted(SENSITIVE_NAMES - {".env"}):
        patterns.append(rf"/{re.escape(name)}$")
    for suffix in sorted(SENSITIVE_SUFFIXES):
        patterns.append(rf"{re.escape(suffix)}$")
    for name in sorted(SENSITIVE_DIRS):
        patterns.append(rf"/{re.escape(name)}")
    # 去重但保持顺序（便于测试与文档核对）
    seen: list[str] = []
    for item in patterns:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def is_protected(path: Path, protected_roots: tuple[Path, ...]) -> bool:
    """是否落在受保护范围（机器人自身源码/配置/数据库等）。"""
    if is_sensitive(path):
        return True
    return any(is_within(path, root) for root in protected_roots)


def relative_label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return str(path)
