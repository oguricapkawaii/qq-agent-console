"""受限目录查找：只在配置授权搜索根内做有上限的本地名称搜索。

约束：
- 不扫描整机、不自动调用模型、不上传候选文件内容；
- 限制深度、候选数、访问条目数与耗时；
- 不跟随符号链接目录（避免以链接方式跳出授权根）；
- 命中常见项目标志（README/.git/pyproject 等）时提高排序，不做语义猜测。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.workspaces.models import DirectoryCandidate

PROJECT_MARKERS = (
    "readme.md", "readme", "pyproject.toml", "package.json", "cargo.toml",
    "go.mod", "pom.xml", "build.gradle", ".git", "requirements.txt",
)

DEFAULT_EXCLUDES = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    "dist", "build", ".mypy_cache", ".ruff_cache", "target", ".idea", ".vscode",
    "Library", ".Trash", ".cache",
})


@dataclass(frozen=True, slots=True)
class SearchBudget:
    max_depth: int = 4
    max_candidates: int = 8
    max_entries: int = 20000
    max_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class SearchReport:
    candidates: tuple[DirectoryCandidate, ...]
    scanned_entries: int
    elapsed_ms: int
    truncated: bool


def _score(name: str, keyword: str, markers: frozenset[str]) -> tuple[int, str]:
    lowered = name.lower()
    key = keyword.lower()
    score = 0
    reasons: list[str] = []
    if lowered == key:
        score += 100
        reasons.append("名称完全匹配")
    elif lowered.startswith(key):
        score += 60
        reasons.append("名称前缀匹配")
    elif key in lowered:
        score += 40
        reasons.append("名称包含关键词")
    if key.replace(" ", "") in lowered.replace(" ", ""):
        score += 10
    if markers:
        score += 5
        reasons.append("含项目标志文件")
    return score, "、".join(reasons) or "名称相似"


def find_directories(
    keyword: str,
    roots: tuple[Path, ...],
    *,
    budget: SearchBudget = SearchBudget(),
    excludes: frozenset[str] = DEFAULT_EXCLUDES,
) -> SearchReport:
    """在授权搜索根内查找名字与关键词相关的目录（有界、可预期）。"""
    key = (keyword or "").strip()
    if not key:
        return SearchReport((), 0, 0, False)
    started = time.monotonic()
    scanned = 0
    truncated = False
    hits: list[DirectoryCandidate] = []
    seen: set[Path] = set()

    for root in roots:
        try:
            root_real = Path(os.path.realpath(str(root)))
        except OSError:
            continue
        if not root_real.is_dir():
            continue
        # 根目录本身也可以命中
        stack: list[tuple[Path, int]] = [(root_real, 0)]
        while stack:
            current, depth = stack.pop()
            if time.monotonic() - started > budget.max_seconds:
                truncated = True
                break
            if depth > budget.max_depth:
                continue
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            scanned += len(entries)
            if scanned > budget.max_entries:
                truncated = True
                break
            markers_present = False
            subdirs: list[Path] = []
            for entry in entries:
                name = entry.name
                if name in excludes:
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir:
                    if name.lower() in PROJECT_MARKERS:
                        markers_present = True
                    continue
                subdirs.append(Path(entry.path))
            if True:  # 根目录本身也可能是用户要找的目录
                score, reason = _score(current.name, key, frozenset({"marker"}) if markers_present else frozenset())
                if score > 0 and current not in seen:
                    seen.add(current)
                    hits.append(
                        DirectoryCandidate(
                            path=current, label=current.name, score=score, reason=reason
                        )
                    )
                    if len(hits) >= budget.max_candidates * 4:
                        truncated = True
                        break
            for child in sorted(subdirs):
                stack.append((child, depth + 1))
        if truncated:
            break

    hits.sort(key=lambda item: (-item.score, str(item.path)))
    candidates = tuple(hits[: budget.max_candidates])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return SearchReport(candidates, scanned, elapsed_ms, truncated)
