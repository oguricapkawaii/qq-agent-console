"""基于根目录 fd 的安全打开原语（R04/R05 的公共机制）。

问题：先 `realpath()`/授权检查、再按**绝对路径字符串** `os.open()`，中间存在检查/使用
竞态——并发进程可以在检查之后把父目录换成指向外部的符号链接，最后的 `O_NOFOLLOW`
只保护最后一级文件名，挡不住父链替换。

本模块的机制：
- 从**可信根的目录 fd** 出发，逐级用 `O_NOFOLLOW|O_DIRECTORY` 打开每一级目录，
  每一步都基于上一级的 `dir_fd`；
- 所有实际文件操作（stat / open / scandir / mkdir / rename / unlink）都使用 `dir_fd`
  相对形式，不再重新解析绝对路径字符串；
- 因此"检查过的对象"与"实际操作的对象"是同一个目录项：即使父目录在检查之后被替换或
  指向别处，我们已经持有的 fd 仍然指向原来的目录 inode，操作不会跑到工作区之外；
- 任何一级遇到符号链接都返回 `ELOOP`（`SymlinkRejected`），由调用方转成明确失败。

被 read / list / search / 备份 / 原子替换 / 删除共同使用。
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
CREATE_FILE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
)

MAX_COMPONENT_BYTES = 255
MAX_RELATIVE_DEPTH = 64


class SafePathError(OSError):
    """安全打开路径失败；message 可直接回执。"""

    def __init__(self, message: str, *, errno_value: int = errno.EACCES) -> None:
        super().__init__(errno_value, message)
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 继承 OSError 的格式化
        return self.message


class SymlinkRejected(SafePathError):
    """路径中出现符号链接：安全打开必须拒绝，而不是跟随。"""

    def __init__(self, message: str = "路径中包含符号链接，已拒绝跟随。") -> None:
        super().__init__(message, errno_value=errno.ELOOP)


def normalize_relative(raw: object) -> tuple[str, ...]:
    """把不可信相对路径拆成安全的组件元组。

    拒绝：空、绝对路径、`..`、`~`、NUL、超长组件、超深路径。
    `.` 与重复斜杠被丢弃（`.` 表示根本身）。
    """
    text = str(raw if raw is not None else ".").strip()
    if not text:
        text = "."
    if "\x00" in text:
        raise SafePathError("路径包含 NUL 字节。")
    if text.startswith("~"):
        raise SafePathError("不支持 ~ 展开；请写工作目录内的相对路径。")
    if text.startswith("/"):
        raise SafePathError("只接受工作目录内的相对路径。")
    parts: list[str] = []
    for chunk in text.split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            raise SafePathError("路径不允许包含 ..")
        if len(chunk.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise SafePathError("路径组件过长。")
        parts.append(chunk)
    if len(parts) > MAX_RELATIVE_DEPTH:
        raise SafePathError(f"路径层级超过上限 {MAX_RELATIVE_DEPTH}。")
    return tuple(parts)


def relative_text(parts: tuple[str, ...]) -> str:
    return "/".join(parts) if parts else "."


class SafeRoot:
    """以某个已存在目录为可信根的安全访问句柄。

    实例应在整个任务期间复用（构造时打开根 fd），这样根自身的身份在整个任务里被钉住，
    即使根路径被并发替换也不会影响后续动作。
    """

    __slots__ = ("_root", "_fd", "_real")

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        resolved = Path(os.path.realpath(str(self._root)))
        flags = DIRECTORY_FLAGS
        try:
            self._fd = os.open(resolved, flags)
        except OSError as exc:  # pragma: no cover - 根目录必须存在且可读
            raise SafePathError(f"无法打开根目录：{exc}") from None
        self._real = resolved

    # ---------- 基本信息 ----------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def real_root(self) -> Path:
        return self._real

    @property
    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            finally:
                self._fd = -1

    def __enter__(self) -> "SafeRoot":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def lexical(self, parts: tuple[str, ...]) -> Path:
        """拼出供授权/敏感判定使用的规范路径（不做 realpath，避免被链接误导）。"""
        return self._real.joinpath(*parts)

    def display(self, parts: tuple[str, ...]) -> str:
        return relative_text(parts)

    # ---------- 逐级打开 ----------

    def _walk(self, parts: tuple[str, ...]) -> int:
        """从根 fd 逐级打开目录；返回**父目录**的 fd（调用方负责关闭）。

        任何一级是符号链接或不是目录都抛错。
        """
        current = os.dup(self._fd)
        for depth, name in enumerate(parts, start=1):
            try:
                nxt = os.open(name, DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                os.close(current)
                raise
            except OSError as exc:
                os.close(current)
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise SymlinkRejected(
                        f"路径第 {depth} 级不是普通目录（可能是符号链接）：{name}"
                    ) from None
                raise SafePathError(f"无法打开路径组件 {name}（{exc.strerror}）。") from None
            # 上一级已不再需要：先接管新 fd，再关闭旧的，避免异常路径上的二次关闭
            previous, current = current, nxt
            os.close(previous)
        return current

    def open_dir(self, parts: tuple[str, ...]) -> int:
        """安全打开目录本身（拒绝符号链接）；返回需要关闭的 fd。"""
        return self._walk(parts)

    def open_parent(self, parts: tuple[str, ...]) -> tuple[int, str]:
        """安全打开父目录；返回 (父目录 fd, 末级名字)。"""
        if not parts:
            raise SafePathError("根目录没有父级。")
        return self._walk(parts[:-1]), parts[-1]

    # ---------- 元数据（不跟随符号链接） ----------

    def lstat(self, parts: tuple[str, ...]) -> os.stat_result:
        """按目录项取元数据（不跟随末级符号链接；中间级必须是普通目录）。"""
        if not parts:
            return os.fstat(self._fd)
        parent, name = self.open_parent(parts)
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)

    def stat_regular(self, parts: tuple[str, ...]) -> os.stat_result:
        """取普通文件元数据；符号链接/目录/特殊文件一律抛错。"""
        info = self.lstat(parts)
        if stat.S_ISLNK(info.st_mode):
            raise SymlinkRejected("目标是符号链接，已拒绝跟随。")
        if not stat.S_ISREG(info.st_mode):
            raise SafePathError("目标不是普通文件。")
        return info

    def exists(self, parts: tuple[str, ...]) -> bool:
        try:
            self.lstat(parts)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return False
        return True

    def is_dir(self, parts: tuple[str, ...]) -> bool:
        try:
            info = self.lstat(parts)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode)

    # ---------- 打开与读 ----------

    def open_regular(self, parts: tuple[str, ...], *, flags: int = FILE_FLAGS) -> int:
        """安全打开普通文件；返回需要关闭的 fd。"""
        parent, name = self.open_parent(parts)
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise SymlinkRejected("目标是符号链接，已拒绝跟随。") from None
            if exc.errno == errno.EISDIR:
                raise SafePathError("目标是一个目录。") from None
            raise SafePathError(f"无法打开文件（{exc.strerror}）。") from None
        finally:
            os.close(parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise SafePathError("目标不是普通文件。")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def read_bytes(self, parts: tuple[str, ...], *, limit: int) -> tuple[bytes, int, bool]:
        """读取有界**内容**（不计算版本）；返回 (data, 文件大小, 是否被截断)。"""
        descriptor = self.open_regular(parts)
        try:
            info = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = max(0, int(limit))
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        return data, info.st_size, info.st_size > len(data)

    def read_with_version(
        self, parts: tuple[str, ...], *, limit: int, with_version: bool = True
    ) -> tuple[bytes, "FileVersion | None", bool]:
        """单次遍历同时得到有界内容与全文件版本（R06）。

        返回 (返回内容, 全文件版本或 None, 是否截断)。内容与版本都来自同一个安全句柄，
        因此不存在"读到的内容"和"记录的版本"来自两次打开、互不一致的窗口。

        `with_version=False` 时只做内容读取（版本记为 None，调用方**不得**把它当作
        文件版本用于漂移判定）；`with_version=True` 且文件超过 `version_read_limit()`
        时明确失败，而不是把片段摘要冒充成版本。
        """
        descriptor = self.open_regular(parts)
        try:
            info = os.fstat(descriptor)
            wanted = max(0, int(limit))
            if not with_version:
                chunks: list[bytes] = []
                remaining = wanted
                while remaining > 0:
                    chunk = os.read(descriptor, min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                return data, None, info.st_size > len(data)
            ceiling = version_read_limit()
            if info.st_size > ceiling:
                raise SafePathError(
                    f"文件 {info.st_size} 字节，超过可安全读取并计算版本的上限 {ceiling} 字节；"
                    "已拒绝读取，请改用 search 或缩小文件。"
                )
            digest = hashlib.sha256()
            chunks = []
            collected = 0
            while True:
                chunk = os.read(descriptor, 262144)
                if not chunk:
                    break
                digest.update(chunk)
                if collected < wanted:
                    take = min(wanted - collected, len(chunk))
                    chunks.append(chunk[:take])
                    collected += take
        finally:
            os.close(descriptor)
        data = b"".join(chunks)
        version = FileVersion(digest.hexdigest(), info.st_size)
        return data, version, info.st_size > len(data)

    def digest(self, parts: tuple[str, ...]) -> "FileVersion":
        """只计算全文件版本（读取整个文件）。"""
        _, version, _ = self.read_with_version(parts, limit=0, with_version=True)
        assert version is not None
        return version

    # ---------- 写入与遍历 ----------

    def create_exclusive(
        self, parts: tuple[str, ...], *, mode: int = 0o600, flags: int = CREATE_FILE_FLAGS
    ) -> int:
        """在安全父目录下以 O_EXCL 独占创建文件；已存在则抛 FileExistsError。"""
        parent, name = self.open_parent(parts)
        try:
            return os.open(name, flags, mode, dir_fd=parent)
        finally:
            os.close(parent)

    def mkdir(self, parts: tuple[str, ...], *, mode: int = 0o700) -> None:
        """安全地逐级创建目录（不跟随符号链接）；已存在时校验它确实是目录。"""
        if not parts:
            return
        current = os.dup(self._fd)
        try:
            for depth, name in enumerate(parts, start=1):
                try:
                    nxt = os.open(name, DIRECTORY_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    try:
                        os.mkdir(name, mode, dir_fd=current)
                    except FileExistsError:
                        pass
                    try:
                        nxt = os.open(name, DIRECTORY_FLAGS, dir_fd=current)
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            raise SymlinkRejected(
                                f"创建目录时发现符号链接：{name}（第 {depth} 级）"
                            ) from None
                        raise SafePathError(f"无法创建目录 {name}（{exc.strerror}）。") from None
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise SymlinkRejected(
                            f"目录路径第 {depth} 级不是普通目录（可能是符号链接）：{name}"
                        ) from None
                    raise SafePathError(f"无法打开目录 {name}（{exc.strerror}）。") from None
                os.close(current)
                current = nxt
        finally:
            os.close(current)

    def scandir(self, parts: tuple[str, ...]) -> list[os.DirEntry[str]]:
        """安全枚举目录项（目录本身拒绝符号链接）。"""
        descriptor = self.open_dir(parts)
        try:
            with os.scandir(descriptor) as iterator:
                return sorted(iterator, key=lambda item: item.name)
        finally:
            os.close(descriptor)

    def child(self, parts: tuple[str, ...], name: str) -> tuple[str, ...]:
        return (*parts, name)


# 单文件安全读取上限：与 broker 的预算分开，防止把超大文件整体读入内存
_VERSION_READ_LIMIT = 8 * 1024 * 1024


def set_version_read_limit(value: int) -> None:
    """调整安全读取上限（测试与部署方显式调整时使用）。"""
    global _VERSION_READ_LIMIT
    _VERSION_READ_LIMIT = max(4096, int(value))


def version_read_limit() -> int:
    return _VERSION_READ_LIMIT


@dataclass(frozen=True, slots=True)
class FileVersion:
    """一次安全读取得到的**全文件**版本信息。"""

    sha256: str
    size: int

    @property
    def known(self) -> bool:
        return bool(self.sha256)
