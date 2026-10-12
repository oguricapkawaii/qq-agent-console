"""受控文件变更：集合、哈希、备份、冲突检测与部分失败记录。

设计要点（统一架构第 4 节 + R04/R05/R06 整改）：
- 每个变更记录原哈希与结果哈希，提交前重新校验，避免"读后被改"的覆盖漂移；
- 覆盖前备份原文件；备份失败即取消该文件写入（不静默丢内容）；
- **所有打开都基于根目录 fd 逐级无跟随进行**（`app.security.safeopen.SafeRoot`）：
  * 备份根 → 任务目录 → 备份文件的整条父链都用 `O_NOFOLLOW|O_DIRECTORY` 逐级打开，
    任何一级是符号链接就拒绝并取消该文件的覆盖（R04）；
  * 原子替换用 `os.replace(..., src_dir_fd=..., dst_dir_fd=...)`、删除用
    `os.unlink(..., dir_fd=...)`，不再按绝对路径字符串操作（R05）；
- 逐文件事务：单个文件用 临时文件 + 原子替换；备份用 `O_EXCL` 唯一命名，禁止覆盖已有文件；
- 多文件提交不是整体原子操作：失败与取消都可能留下部分写入，
  因此逐文件记录 status/error，并提供恢复方法说明，不承诺整体回滚；
- 内容摘要与**文件版本**分开：登记与复核一律使用全文件 SHA256（R06）。
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import logging
import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.security.safeopen import (
    SafePathError,
    SafeRoot,
    SymlinkRejected,
    normalize_relative,
)

logger = logging.getLogger("qqbot.workspaces.changes")

KIND_CREATE = "create"
KIND_MODIFY = "modify"
KIND_DELETE = "delete"

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_CONFLICT = "conflict"
STATUS_SKIPPED = "skipped"

_MAX_DIFF_LINES = 40

# 同一文件在同一任务里被反复修改（例如 team retry）时，备份文件名的递增上限
_MAX_BACKUP_VARIANTS = 1000


class ChangeError(ValueError):
    """变更被拒绝；message 可直接回执。"""


def file_hash(path: Path) -> str:
    """文件内容 SHA256；文件不存在返回空串。"""
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 256):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class FileChange:
    """一次计划中的文件变更及其执行结果。"""

    path: Path
    kind: str
    content: str | None = None
    expected_hash: str | None = None
    before_hash: str = ""
    after_hash: str = ""
    backup: str = ""
    bytes_written: int = 0
    status: str = STATUS_PENDING
    error: str = ""
    diff_summary: str = ""

    @property
    def display(self) -> str:
        return str(self.path)


@dataclass(slots=True)
class ChangeSet:
    """一组变更；由调用方在持有目录锁期间 apply。"""

    root: Path
    task_id: str = ""
    max_bytes: int = 256 * 1024
    # 备份落到受控目录（默认目标文件同目录的 .bak 已废弃：会跟随符号链接写出边界）
    backup_root: Path | None = None
    guard: Callable[[], None] | None = None
    changes: list[FileChange] = field(default_factory=list)
    # 可信根的安全句柄：整个提交过程复用，根身份被钉住（并发替换不会改变目标）
    safe_root: SafeRoot | None = None
    # 备份根的安全句柄：**构造时绑定一次**，之后不再 realpath 重新接纳新目标（T02）
    backup_safe_root: SafeRoot | None = None

    def __post_init__(self) -> None:
        if self.backup_safe_root is None and self.backup_root is not None:
            self.backup_safe_root = open_backup_root(self.backup_root)

    # ---------- 安全句柄 ----------

    def _safe(self) -> SafeRoot:
        if self.safe_root is None:
            self.safe_root = SafeRoot(self.root)
        return self.safe_root

    def close(self) -> None:
        for handle in (self.safe_root, self.backup_safe_root):
            if handle is not None:
                handle.close()
        self.safe_root = None
        self.backup_safe_root = None

    def _parts(self, target: Path) -> tuple[str, ...]:
        """把目标路径换算成相对可信根的组件元组。"""
        try:
            relative = target.relative_to(self.root)
        except ValueError:
            raise ChangeError("目标不在本次任务的授权目录内，已拒绝。") from None
        try:
            return normalize_relative("/".join(relative.parts))
        except SafePathError as exc:
            raise ChangeError(f"路径无法安全解析：{exc}") from None

    def _version_of(self, target: Path) -> str:
        """通过安全句柄计算全文件版本；不存在/不是普通文件返回空串。"""
        parts = self._parts(target)
        try:
            return self._safe().digest(parts).sha256
        except (SafePathError, FileNotFoundError, NotADirectoryError):
            return ""
        except OSError:
            return ""

    def _read_text(self, target: Path) -> str:
        parts = self._parts(target)
        try:
            data, _version, _truncated = self._safe().read_with_version(
                parts, limit=self.max_bytes, with_version=False
            )
        except (SafePathError, FileNotFoundError, NotADirectoryError):
            return ""
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")

    # ---------- 计划 ----------

    def add_write(self, path: Path, content: str, *, expected_hash: str | None = None) -> FileChange:
        """登记一次写入。

        F06/R06：**规划时的存在性与全文件版本哈希总是被冻结**，与模型是否提供
        expected_hash 无关；提交前再次复核，外部并发修改会被拒绝而不是覆盖。
        """
        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            raise ChangeError(
                f"{path.name} 内容 {len(data)} 字节，超过单文件上限 {self.max_bytes} 字节。"
            )
        parts = self._parts(path)
        try:
            info = self._safe().lstat(parts)
        except FileNotFoundError:
            info = None
        except (SafePathError, NotADirectoryError) as exc:
            raise ChangeError(f"{path.name} 无法安全访问：{exc}") from None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise ChangeError(f"{path.name} 是符号链接，已拒绝通过链接写入。")
        exists = info is not None and stat.S_ISREG(info.st_mode)
        if info is not None and not exists:
            raise ChangeError(f"{path.name} 是目录或特殊文件，不能写入。")
        change = FileChange(
            path=path,
            kind=KIND_MODIFY if exists else KIND_CREATE,
            content=content,
            expected_hash=expected_hash,
        )
        if exists:
            change.before_hash = self._version_of(path)
            if not change.before_hash:
                raise ChangeError(f"{path.name} 的版本无法读取，已拒绝覆盖。")
            if expected_hash is not None and change.before_hash != expected_hash:
                raise ChangeError(
                    f"{path.name} 在你读取之后已被改动（哈希漂移），已拒绝覆盖；请重新读取。"
                )
        elif expected_hash:
            raise ChangeError(f"{path.name} 不存在，但提供了预期哈希；已拒绝。")
        change.after_hash = text_hash(content)
        change.diff_summary = self._diff(path, content)
        self.changes.append(change)
        return change

    def add_delete(self, path: Path, *, expected_hash: str | None = None) -> FileChange:
        """登记一次删除；规划时冻结全文件版本，提交前复核（F06/R06）。"""
        before = self._version_of(path)
        if not before:
            raise ChangeError(f"{path.name} 不存在，无法删除。")
        change = FileChange(
            path=path, kind=KIND_DELETE, expected_hash=expected_hash, before_hash=before,
        )
        if expected_hash is not None and change.before_hash != expected_hash:
            raise ChangeError(f"{path.name} 已被改动（哈希漂移），已拒绝删除；请重新读取。")
        self.changes.append(change)
        return change

    def _diff(self, path: Path, content: str) -> str:
        previous = self._read_text(path)
        return render_diff(previous, content, path.name)

    # ---------- 执行 ----------

    def apply(
        self, *, authorize: Callable[[Path], None] | None = None,
        backup_root: Path | None = None,
    ) -> list[FileChange]:
        """逐文件执行；返回带最终状态的变更列表（含失败项）。

        `backup_root` 未显式给出时使用构造时的 backup_root；两处都为空则退回目标同目录
        `.bak`（仅在调用方明确允许时，测试/兼容路径），并始终拒绝跟随已有符号链接。
        """
        root = backup_root if backup_root is not None else self.backup_root
        for change in self.changes:
            target = change.path
            try:
                if self.guard is not None:
                    self.guard()          # F05：取消/终态复查
                if authorize is not None:
                    authorize(target)
                if change.kind == KIND_DELETE:
                    self._apply_delete(change, root)
                else:
                    self._apply_write(change, root)
            except (ChangeError, OSError, PermissionError) as exc:
                # 冲突（版本漂移/新建目标已存在）保持 conflict，其余记为 failed
                if change.status != STATUS_CONFLICT:
                    change.status = STATUS_FAILED
                change.error = str(exc) or "写入失败"
                logger.warning("变更失败 path=%s error=%s", target, change.error)
        return list(self.changes)

    def _apply_write(self, change: FileChange, backup_root: Path | None) -> None:
        target = change.path
        parts = self._parts(target)
        try:
            info = self._safe().lstat(parts)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            raise ChangeError("目标是目录，不能写入。")
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise ChangeError("目标已被替换成符号链接，本次未写入。")
        # F06/R06：规划版本总是被强制复核（不依赖模型是否给了 expected_sha256）
        if change.before_hash:
            if info is None:
                change.status = STATUS_CONFLICT
                raise ChangeError("文件在提交前被删除或被替换，本次未写入。")
            if self._version_of(target) != change.before_hash:
                change.status = STATUS_CONFLICT
                raise ChangeError("文件在提交前被外部修改（版本漂移），本次未写入。")
        elif info is not None:
            change.status = STATUS_CONFLICT
            raise ChangeError("规划时不存在的新建目标在提交前已出现，已拒绝覆盖。")
        if change.expected_hash is not None and info is not None:
            if self._version_of(target) != change.expected_hash:
                change.status = STATUS_CONFLICT
                raise ChangeError("文件在提交前发生变化（哈希漂移），本次未写入。")
        self._safe().mkdir(parts[:-1])
        if info is not None:
            # R04：备份不安全时直接取消本次覆盖（不写盘）
            change.backup = self._backup(target, backup_root)
        data = (change.content or "").encode("utf-8")
        self._atomic_write(parts, data)
        change.bytes_written = len(data)
        change.after_hash = text_hash(change.content or "")
        change.status = STATUS_APPLIED

    def _apply_delete(self, change: FileChange, backup_root: Path | None) -> None:
        target = change.path
        parts = self._parts(target)
        try:
            info = self._safe().lstat(parts)
        except FileNotFoundError:
            change.status = STATUS_CONFLICT
            raise ChangeError("文件在提交前已被删除，本次未执行删除。") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            change.status = STATUS_CONFLICT
            raise ChangeError("目标已不是普通文件，本次未执行删除。")
        if change.before_hash and self._version_of(target) != change.before_hash:
            change.status = STATUS_CONFLICT
            raise ChangeError("文件在提交前被外部修改（版本漂移），已拒绝删除。")
        change.backup = self._backup(target, backup_root)
        parent = self._safe().open_parent(parts)[0]
        try:
            os.unlink(parts[-1], dir_fd=parent)
        finally:
            os.close(parent)
        change.status = STATUS_APPLIED

    def _backup(self, target: Path, backup_root: Path | None) -> str:
        """把原文件复制到受控备份位置（R04 + T02）。

        备份根 → 任务目录 → 备份文件名**整条父链**都用"根 fd + 逐级无跟随"打开：
        任何一级是符号链接或身份被替换，都抛 ChangeError 取消该文件的覆盖，
        而不是把备份写到外面。备份文件以 `O_EXCL` 独占创建、唯一命名，
        禁止覆盖任何已有文件（F04）。

        备份根本身使用 `backup_safe_root`（构造时打开、身份钉住，T02）：
        提交期间有人把备份根路径换成指向别处的链接，也不会被跟随——
        我们会继续往原来那个目录 inode 里写，且它不可达时直接失败。
        """
        parts = self._parts(target)
        # 备份文件名沿用旧约定"相对路径里的 / 换成 __"：同一任务里不同子目录的同名文件
        # 不会互相挤占；同一文件被多次修改（team retry）时用 .001、.002 … 递增，最多 1000 个。
        name = "/".join(parts).replace("/", "__").replace("\\", "__") + ".bak"
        writer: _BackupWriter
        if self.backup_safe_root is not None:
            # T02：用**构造时绑定**的备份根句柄，绝不在此重新解析路径
            writer = _BackupWriter(self.backup_safe_root, (self.task_id or "task",), owned=False)
        else:
            # 默认备份位置在工作区内（<父目录>/.qq-backups/<task>）：
            # 仍走同一套安全打开，只是可信根换成任务根
            writer = _BackupWriter(
                self._safe(), (*parts[:-1], ".qq-backups", self.task_id or "task"),
                owned=False,  # 复用 ChangeSet 的可信根，别把共享 fd 关掉
            )
        allocated_name = ""
        try:
            descriptor, allocated_name = writer.allocate(name)
            try:
                with os.fdopen(descriptor, "wb") as output:  # fd 的关闭责任交给它
                    source = self._safe().open_regular(parts)
                    with os.fdopen(source, "rb") as handle:
                        while True:
                            chunk = handle.read(262144)
                            if not chunk:
                                break
                            output.write(chunk)
            except BaseException:
                writer.unlink(allocated_name)
                raise
            return writer.display(allocated_name)
        except SymlinkRejected as exc:
            raise ChangeError(
                f"备份路径不安全（{exc.message}），已取消该文件的覆盖。"
            ) from None
        except SafePathError as exc:
            raise ChangeError(f"备份原文件失败，已取消写入（{exc.message}）") from None
        except OSError as exc:
            raise ChangeError(f"备份原文件失败，已取消写入（{exc}）") from None
        finally:
            writer.close()

    def _atomic_write(self, parts: tuple[str, ...], data: bytes) -> None:
        """同目录临时文件 + `os.replace(dir_fd=...)` 原子替换（不按路径字符串操作）。"""
        safe = self._safe()
        parent = safe.open_parent(parts)[0]
        name = parts[-1]
        temp_name = ""
        try:
            for attempt in range(20):
                candidate = f".qq-{os.getpid()}-{os.urandom(6).hex()}"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent,
                    )
                except FileExistsError:  # pragma: no cover - 名称冲突概率极低
                    continue
                temp_name = candidate
                break
            else:  # pragma: no cover - 极端情况
                raise ChangeError("无法创建临时文件，已取消写入。")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            temp_name = ""
        except OSError as exc:
            raise ChangeError(f"写入失败：{exc}") from None
        finally:
            if temp_name:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name, dir_fd=parent)
            os.close(parent)

    # ---------- 摘要 ----------

    def applied(self) -> list[FileChange]:
        return [change for change in self.changes if change.status == STATUS_APPLIED]

    def failed(self) -> list[FileChange]:
        return [change for change in self.changes if change.status == STATUS_FAILED]

    def summary_lines(self, *, limit: int = 12) -> list[str]:
        lines: list[str] = []
        for change in self.changes[:limit]:
            mark = {
                KIND_CREATE: "新建", KIND_MODIFY: "修改", KIND_DELETE: "删除",
            }.get(change.kind, change.kind)
            state = {
                STATUS_APPLIED: "已应用", STATUS_FAILED: f"失败：{change.error}",
                STATUS_CONFLICT: "冲突未写入", STATUS_SKIPPED: "跳过", STATUS_PENDING: "未执行",
            }.get(change.status, change.status)
            detail = f"（{change.bytes_written} 字节" if change.bytes_written else "（"
            if change.backup:
                detail += f"，备份 {Path(change.backup).name}"
            detail += "）"
            lines.append(f"{mark} {change.path.name} {state}{detail}")
        if len(self.changes) > limit:
            lines.append(f"…… 还有 {len(self.changes) - limit} 个文件未列出")
        return lines

    def public_payload(self) -> list[dict]:
        return [
            {
                "path": str(change.path),
                "kind": change.kind,
                "status": change.status,
                "before_hash": change.before_hash,
                "after_hash": change.after_hash,
                "backup": change.backup,
                "bytes": change.bytes_written,
                "error": change.error,
            }
            for change in self.changes
        ]


def open_backup_root(backup_root: Path) -> SafeRoot:
    """补建受控备份根并**打开一次**，返回身份固定（钉子化）的安全句柄。

    备份失败会**取消该文件的覆盖**，因此"备份根不存在"不能等价于"任务里的写入全部失败"；
    这里先补齐目录（`mkdir -p`），再打开根目录 fd。

    返回的句柄由调用方（`FileBroker` / `ChangeSet`）在任务期间持有，备份一律通过它进行——
    **不再按路径重新解析**。这样即使有人在本任务提交期间把备份根路径换成指向别处的符号链接，
    我们也只会在原来那个目录 inode 里创建文件（或因为该目录已被移走而明确失败），
    不会"跟在最新路径后面"把备份写到受控范围之外（T02）。
    """
    resolved = Path(os.path.realpath(str(backup_root)))
    if not resolved.is_dir():
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # 交给 SafeRoot 报出可读原因
    return SafeRoot(resolved)


class _BackupWriter:
    """在受控备份根下安全创建备份文件（R04）。

    - 目录链用 `SafeRoot.mkdir()` 逐级创建/打开：任何一级是符号链接就抛
      `SymlinkRejected`，绝不跟随；
    - 备份文件用 `O_EXCL|O_NOFOLLOW` 独占创建；同名时按 `.001`、`.002` 换名，绝不覆盖；
    - `close()` 释放根 fd。
    """

    __slots__ = ("_safe", "_directory", "_owned")

    def __init__(self, safe: SafeRoot, directory: tuple[str, ...], *, owned: bool = True) -> None:
        self._safe = safe
        self._directory = tuple(directory)
        self._owned = owned

    def _prepare(self) -> None:
        if self._directory:
            self._safe.mkdir(self._directory)

    def allocate(self, *name: str) -> tuple[int, str]:
        """独占创建备份文件；返回 (需要关闭的 fd, 实际文件名)。"""
        self._prepare()
        base = name[0]
        for counter in range(0, _MAX_BACKUP_VARIANTS):
            # base 已经带 `.bak` 后缀；同名再用 `.001`、`.002`…（补零便于按序查看）
            candidate = base if counter == 0 else f"{base}.{counter:03d}"
            parts = (*self._directory, candidate)
            try:
                return self._safe.create_exclusive(parts), candidate
            except FileExistsError:
                continue
        raise ChangeError(
            f"备份目录里已有 {_MAX_BACKUP_VARIANTS} 个同名备份，已取消写入；"
            "请清理该任务的备份目录后重试。"
        )

    def unlink(self, name: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self.display(name))

    def display(self, name: str) -> str:
        return str(self._safe.real_root.joinpath(*self._directory, name))

    def close(self) -> None:
        if self._owned:
            self._safe.close()


def render_diff(before: str, after: str, name: str) -> str:
    """有界的统一 diff 摘要（避免把整个文件塞进群消息/上下文）。"""
    diff = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="", n=1,
        )
    )
    if len(diff) > _MAX_DIFF_LINES:
        diff = diff[:_MAX_DIFF_LINES] + ["……（差异过长，已截断）"]
    return "\n".join(diff)


def hash_many(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): file_hash(path) for path in paths}


_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".qq-backups"})


def snapshot_workspace(root: Path, *, limit: int = 2000) -> dict[str, str]:
    """有界工作区快照：相对路径 -> sha256。

    用于记录"命令直接产生的文件改动"（exec 没有逐文件 API）与原生工具模式的事后审计。
    超过 limit 即停止（返回部分快照，并在结果里标记不完整）。
    """
    snapshot: dict[str, str] = {}
    stack = [Path(root)]
    while stack and len(snapshot) < limit:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if len(snapshot) >= limit:
                break
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _SKIP_DIRS:
                        continue
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            try:
                snapshot[str(path.relative_to(root))] = file_hash(path)
            except (OSError, ValueError):
                continue
    return snapshot


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """返回 [{path, kind, before_hash, after_hash}]（kind: create/modify/delete）。"""
    rows: list[dict] = []
    for path, digest in sorted(after.items()):
        if path not in before:
            rows.append({"path": path, "kind": KIND_CREATE, "before_hash": "",
                         "after_hash": digest})
        elif before[path] != digest:
            rows.append({"path": path, "kind": KIND_MODIFY, "before_hash": before[path],
                         "after_hash": digest})
    for path, digest in sorted(before.items()):
        if path not in after:
            rows.append({"path": path, "kind": KIND_DELETE, "before_hash": digest,
                         "after_hash": ""})
    return rows
