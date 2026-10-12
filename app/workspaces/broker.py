"""公共文件 broker：所有后端都能通过结构化动作获得 read/edit 能力。

工作方式（统一架构第 4 节 + F03/F12 + R03/R05/R06 整改）：
- 模型只返回**数据**（JSON 动作），服务端严格校验后才真正读写文件；
- 支持的动作为 list / read / search / propose_changes / run_command / final；
- 路径安全统一走 `_SafeAccess`（`app.security.safeopen.SafeRoot`）：
  * 从**根目录 fd** 出发逐级 `O_NOFOLLOW|O_DIRECTORY` 打开，所有 stat/open/scandir 都用
    `dir_fd` 相对形式，检查过的对象与实际操作的对象是同一个目录项，
    父目录在检查之后被替换也不会把操作引到工作区之外；
  * 每个后代路径（list/search 的每一项）在打开前都重新做**最具体授权**、受保护与敏感复查，
    父目录授权不会覆盖更具体的子目录授权，实时撤权立即生效；
  * 符号链接不参与 list/search，也不作为别名绕过敏感清单；
- 读取有预算：单文件上限、累计字节上限、列表条目上限、搜索条目/命中上限，
  超限即明确失败或停止（不静默截断后继续）；
- 内容摘要与**文件版本**是两件事（R06）：read/search 返回的 `sha256` 是**返回内容**的摘要，
  `version_sha256` 是与内容同一次安全读取得到的**全文件**版本，只有后者会用于漂移判定；
- read 模式下写动作是硬拒绝（BrokerFatal），不是提示模型；
- propose_changes 只登记变更，真正落盘由 `commit()` 在持有目录锁时执行，
  逐文件记录哈希、备份与结果。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from app.security.acl import AclService
from app.security.exec_policy import ExecPolicy, run_command
from app.security.paths import is_protected, is_sensitive, is_within
from app.security.safeopen import (
    SafePathError,
    SafeRoot,
    SymlinkRejected,
    normalize_relative,
    relative_text,
)
from app.workspaces.changes import (
    ChangeError,
    ChangeSet,
    FileChange,
    diff_snapshots,
    file_hash,
    snapshot_workspace,
    text_hash,
)

logger = logging.getLogger("qqbot.workspaces.broker")

WRITE_MODES = frozenset({MODE_EDIT, MODE_EXEC})

# 允许的动作白名单：任何其他名字都是错误，绝不执行模型返回的代码
ALLOWED_ACTIONS = ("list", "read", "search", "propose_changes", "run_command", "final")


@dataclass(frozen=True, slots=True)
class ExecPathRule:
    """exec 沙箱对某个子树的读写限制（按当前用户的实际有效能力计算）。

    `allow_read`/`allow_write` 是**沙箱层面**的允许位：False 表示追加 deny 规则，
    因为 broker 侧的 ACL 已经拒绝了该用户在这个子树上的对应动作。
    """

    path: Path
    allow_read: bool
    allow_write: bool


def _real_path(value: Path) -> Path:
    try:
        return Path(os.path.realpath(str(value)))
    except OSError:  # pragma: no cover - 极端平台
        return Path(value)


@dataclass(slots=True)
class BrokerBudget:
    max_actions: int = 40
    max_rounds: int = 8
    max_total_read_bytes: int = 256 * 1024
    max_file_bytes: int = 128 * 1024
    max_list_entries: int = 200
    max_search_hits: int = 40
    # F12：搜索的条目预算（不仅是命中数）与二进制/超限处理
    max_search_entries: int = 2000


@dataclass(slots=True)
class ActionResult:
    ok: bool
    action: str
    payload: dict = field(default_factory=dict)
    error: str = ""
    # fatal=True 表示这是权限/预算/协议级失败：任务必须明确失败，不能继续循环
    fatal: bool = False

    def as_message(self) -> str:
        body = {"action": self.action, "ok": self.ok}
        if self.ok:
            body["result"] = self.payload
        else:
            body["error"] = self.error
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


class BrokerError(ValueError):
    """动作失败；可恢复的错误会回灌给模型继续处理。"""


class BrokerFatal(BrokerError):
    """权限、预算或协议级失败：任务必须明确失败，不得继续循环或降级。"""


class FileBroker:
    """在单一规范目录内执行结构化文件动作。"""

    def __init__(
        self,
        *,
        acl: AclService,
        root: Path,
        group_id: int,
        user_id: int,
        task_id: str,
        mode: str,
        budget: BrokerBudget | None = None,
        exec_policy: ExecPolicy | None = None,
        backup_root: Path | None = None,
        guard: Callable[[], None] | None = None,
    ) -> None:
        self._acl = acl
        # 授权根统一按真实路径保存：否则 macOS 上 `/tmp/...` 与 `/private/...` 这类
        # 别名会让"相对路径换算"失效（拒绝服务而非放行，但仍必须处理）
        self._root = Path(os.path.realpath(str(root)))
        self._group_id = group_id
        self._user_id = user_id
        self._task_id = task_id
        self._mode = mode
        self._budget = budget or BrokerBudget()
        self._exec_policy = exec_policy
        # T02：备份根在**构造时**补建并打开一次，整个任务复用同一个 fd/身份，
        # 提交期间不再 realpath 重新接纳被替换的目标。
        self._backup_root = _ensure_backup_root(backup_root)
        self._guard = guard
        self._actions = 0
        self._read_bytes = 0
        self._budget_exhausted = False
        self._change_set = ChangeSet(
            root=self._root, task_id=task_id, max_bytes=self._budget.max_file_bytes,
            backup_root=backup_root, guard=guard,
        )
        # 记录"被读取过的文件 + 当时哈希"（F10：team retry 的输入漂移判定）
        self._reads: dict[str, str] = {}
        self._commands: list[dict] = []
        # 命令直接产生的文件改动（exec 没有逐文件工具，用有界快照记录）
        self._command_changes: list[dict] = []

    # ---------- 状态 ----------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def can_write(self) -> bool:
        return self._mode in WRITE_MODES

    @property
    def actions_used(self) -> int:
        return self._actions

    @property
    def read_bytes(self) -> int:
        return self._read_bytes

    def pending_changes(self) -> list[FileChange]:
        return list(self._change_set.changes)

    def close(self) -> None:
        """释放 broker 持有的安全根 fd（任务结束后调用，避免长进程里累积 fd）。"""
        with contextlib.suppress(Exception):
            self._change_set.close()

    def __enter__(self) -> "FileBroker":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def command_log(self) -> list[dict]:
        return list(self._commands)

    def command_changes(self) -> list[dict]:
        """exec 命令直接写出的文件改动（相对路径 + 前后哈希）。"""
        return list(self._command_changes)

    def input_fingerprints(self) -> dict[str, str]:
        """本次任务读取过的文件及其哈希（用于 retry 的输入漂移判定）。"""
        return dict(self._reads)

    # ---------- 授权与路径 ----------

    def _authorize(self, path: Path, *, write: bool) -> None:
        """每次文件动作都实时复查授权（撤权立即生效）。"""
        if write and self._mode not in WRITE_MODES:
            raise BrokerFatal(
                f"当前模式是 {self._mode}，不允许修改文件；需要修改请由用户显式使用 --mode edit。"
            )
        decision = self._acl.authorize_path(
            user_id=self._user_id, group_id=self._group_id, path=path, mode=self._mode,
        )
        if not decision.allowed:
            raise BrokerFatal(decision.reason)
        if is_protected(path, self._acl.protected_roots):
            raise BrokerFatal("该路径位于受保护范围（凭据或机器人自身数据），已拒绝。")
        if is_sensitive(path):
            raise BrokerFatal("该路径命中默认敏感清单（凭据/密钥/数据库），已拒绝。")

    def _resolve(self, raw: str | None, *, must_exist: bool = False) -> Path:
        relative = (raw or ".").strip()
        if relative.startswith("~"):
            raise BrokerFatal("不支持 ~ 展开；请写工作目录内的相对路径。")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise BrokerFatal("文件工具只接受工作目录内的相对路径。")
        if any(part == ".." for part in candidate.parts):
            raise BrokerFatal("路径不允许包含 ..")
        target = self._root / candidate
        try:
            target = Path(os.path.realpath(str(target)))
        except OSError:
            raise BrokerFatal("路径无法解析。") from None
        if not is_within(target, self._root):
            raise BrokerFatal("路径超出本次任务的授权目录。")
        if is_sensitive(target):
            raise BrokerFatal("该路径命中默认敏感清单（凭据/密钥/数据库），已拒绝。")
        if must_exist and not target.exists():
            # 可恢复错误：模型可以换一个路径继续（不是权限/预算违规）
            raise BrokerError("文件或目录不存在。")
        return target

    def _safe_parts(self, path: Path) -> tuple[str, ...]:
        """把已解析的绝对路径转成相对于授权根的组件元组。"""
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            raise BrokerFatal("路径超出本次任务的授权目录。") from None
        try:
            return normalize_relative(relative_text(relative.parts))
        except SafePathError:
            raise BrokerFatal("路径无法安全解析。") from None

    def _open_regular(self, path: Path, **kwargs: object) -> object:
        """基于根 fd 的安全打开（不跟随任何一级符号链接），内容与版本一次读齐。

        真实实现见 `_SafeAccess`；保留 `_open_regular` 作为唯一打开点，
        是为了让审计/测试可以在这里注入"检查之后替换父目录"的钩子，
        验证父链替换不再能把读取引到工作区之外。
        """
        parts = kwargs.get("parts")
        limit = int(kwargs.get("limit") or 0)  # type: ignore[arg-type]
        if not isinstance(parts, tuple):
            parts = self._safe_parts(path)
        access = self._access()
        try:
            return access.safe.read_with_version(parts, limit=limit)
        except FileNotFoundError:
            return None
        finally:
            access.close()

    def _access(self) -> "_SafeAccess":
        return _SafeAccess(broker=self, safe=SafeRoot(self._root))

    def _iter_files(self, base: Path) -> Iterator[Path]:
        """在授权范围内安全枚举普通文件：跳过符号链接、敏感、受保护与未授权路径（F03/R03）。"""
        access = self._access()
        try:
            for path in access.iter_files(self._safe_parts(base)):
                yield path
        finally:
            access.close()

    # ---------- 动作分发 ----------

    async def execute(self, action: Mapping[str, object]) -> ActionResult:
        self._actions += 1
        if self._actions > self._budget.max_actions:
            return ActionResult(
                False, str(action.get("action") or "?"),
                error=f"动作数超过上限 {self._budget.max_actions}，任务已停止。",
                fatal=True,
            )
        name = str(action.get("action") or "").strip().lower()
        try:
            if self._guard is not None:
                self._guard()
            if name == "list":
                return self._do_list(action)
            if name == "read":
                return self._do_read(action)
            if name == "search":
                return self._do_search(action)
            if name == "propose_changes":
                return self._do_propose(action)
            if name == "run_command":
                return await self._do_run_command(action)
            if name == "final":
                return ActionResult(True, "final", {"answer": str(action.get("answer") or "")})
            return ActionResult(
                False, name or "?",
                error=f"未知动作 {name!r}；只支持：{', '.join(ALLOWED_ACTIONS)}。",
                fatal=True,
            )
        except BrokerFatal as exc:
            return ActionResult(False, name or "?", error=str(exc), fatal=True)
        except BrokerError as exc:
            return ActionResult(False, name or "?", error=str(exc))
        except (OSError, ValueError, ChangeError) as exc:
            return ActionResult(False, name or "?", error=f"动作失败：{exc}")

    # ---------- 具体动作 ----------

    def _do_list(self, action: Mapping[str, object]) -> ActionResult:
        target = self._resolve(_as_str(action.get("path")) or ".")
        self._authorize(target, write=False)
        if not target.exists():
            raise BrokerError("目录不存在。")
        if target.is_file():
            raise BrokerError("这是一个文件，请用 read。")
        depth = max(1, min(int(action.get("depth") or 1), 3))
        entries: list[dict] = []
        truncated = False
        access = self._access()
        try:
            stack: list[tuple[tuple[str, ...], int]] = [(self._safe_parts(target), 0)]
            while stack:
                parts, level = stack.pop()
                try:
                    children = access.safe.scandir(parts)
                except OSError:
                    continue
                for entry in children:
                    if len(entries) >= self._budget.max_list_entries:
                        truncated = True
                        break
                    child_parts = (*parts, entry.name)
                    label = relative_text(child_parts)
                    try:
                        # 注意：os.scandir(fd) 返回的 DirEntry 无法再按路径 stat，
                        # 因此元数据一律走 dir_fd 相对的安全 lstat。
                        info = access.safe.lstat(child_parts)
                        if stat.S_ISLNK(info.st_mode):
                            # 符号链接只报告自身，不展示它可能指向的工作区外内容
                            entries.append({"path": label, "type": "symlink"})
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            # R03：每个后代路径都重新做最具体授权 + 保护复查
                            if not access.allowed(child_parts, write=False):
                                continue
                            entries.append({"path": label, "type": "dir"})
                            if level + 1 < depth:
                                stack.append((child_parts, level + 1))
                        else:
                            if not access.allowed(child_parts, write=False):
                                continue
                            entries.append({
                                "path": label, "type": "file", "bytes": info.st_size,
                            })
                    except OSError:
                        continue
                if truncated:
                    break
        finally:
            access.close()
        return ActionResult(True, "list", {
            "root": str(self._root), "entries": entries, "truncated": truncated,
        })

    def _do_read(self, action: Mapping[str, object]) -> ActionResult:
        target = self._resolve(_as_str(action.get("path")), must_exist=True)
        self._authorize(target, write=False)
        parts = self._safe_parts(target)
        limit = int(action.get("max_bytes") or self._budget.max_file_bytes)
        limit = max(1, min(limit, self._budget.max_file_bytes))
        remaining = self._budget.max_total_read_bytes - self._read_bytes
        if remaining <= 0:
            return ActionResult(
                False, "read",
                error=f"累计读取超过上限 {self._budget.max_total_read_bytes} 字节，任务已停止。",
                fatal=True,
            )
        access = self._access()
        try:
            # 先确认它确实是普通文件（目录/符号链接/特殊文件都有明确回执）
            try:
                info = access.safe.lstat(parts)
            except FileNotFoundError:
                raise BrokerError("文件或目录不存在。") from None
            except SafePathError as exc:
                raise BrokerFatal(str(exc)) from None
            if stat.S_ISLNK(info.st_mode):
                raise BrokerFatal("目标是符号链接，已拒绝跟随。")
            if stat.S_ISDIR(info.st_mode):
                raise BrokerError("这是一个目录，请用 list。")
            if not stat.S_ISREG(info.st_mode):
                raise BrokerError("读取失败：这不是普通文件。")
            try:
                data, version, truncated = self._read_file(
                    access, target, parts, limit=min(limit, remaining),
                )
            except SymlinkRejected as exc:
                raise BrokerFatal(str(exc)) from None
            except SafePathError as exc:
                raise BrokerError(str(exc)) from None
        finally:
            access.close()
        text = data.decode("utf-8", errors="replace")
        if "\x00" in text:
            raise BrokerError("读取失败：这是二进制文件。")
        self._read_bytes += len(data)
        content_digest = hashlib.sha256(data).hexdigest()
        version_digest = version.sha256 if version is not None else ""
        if version_digest:
            # R06：只有**全文件版本**才进入漂移判定表；片段摘要绝不冒充版本
            self._reads[str(target)] = version_digest
        return ActionResult(True, "read", {
            "path": self._rel(target), "text": text,
            "bytes": len(data), "truncated": truncated,
            "full_bytes": version.size if version is not None else info.st_size,
            "sha256": content_digest,
            "version_sha256": version_digest,
            "version_known": bool(version_digest),
            "version_note": (
                "" if version_digest else
                "本次只返回了有界内容，未取得可用的全文件版本；请在稍后用完整读取再修改。"
            ),
        })

    def _do_search(self, action: Mapping[str, object]) -> ActionResult:
        query = _as_str(action.get("query")) or ""
        if not query:
            raise BrokerError("search 需要 query。")
        base = self._resolve(_as_str(action.get("path")) or ".")
        self._authorize(base, write=False)
        limit = max(1, min(int(action.get("max_results") or self._budget.max_search_hits),
                           self._budget.max_search_hits))
        hits: list[dict] = []
        scanned = 0
        skipped = 0
        budget_stop = False
        is_file = base.is_file()
        access = self._access()
        try:
            candidates: Iterator[Path] = (
                iter([base]) if is_file else access.iter_files(self._safe_parts(base))
            )
            for path in candidates:
                if len(hits) >= limit:
                    break
                if self._budget_exhausted:
                    budget_stop = True
                    break
                remaining = self._budget.max_total_read_bytes - self._read_bytes
                if remaining <= 0:
                    budget_stop = True
                    break
                parts = self._safe_parts(path)
                # R03：每次打开前都重新做最具体授权、保护与敏感复查
                if not access.allowed(parts, write=False):
                    skipped += 1
                    continue
                scanned += 1
                try:
                    payload, version, _truncated = self._read_file(
                        access, path, parts,
                        limit=min(self._budget.max_file_bytes, remaining),
                    )
                except OSError:
                    continue
                self._read_bytes += len(payload)
                if self._read_bytes >= self._budget.max_total_read_bytes:
                    budget_stop = True
                if b"\x00" in payload:
                    continue
                if version is not None:
                    # 只有全文件版本进入漂移判定表
                    self._reads.setdefault(str(path), version.sha256)
                text = payload.decode("utf-8", errors="replace")
                for number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        hits.append({"path": self._rel(path), "line": number, "text": line[:400]})
                        if len(hits) >= limit:
                            break
        finally:
            access.close()
        if budget_stop and not hits:
            return ActionResult(
                False, "search",
                error=(
                    f"搜索读取达到预算上限（累计 {self._read_bytes} 字节 / 条目 {scanned}），"
                    "已停止；请缩小范围或提高 commands.broker 预算。"
                ),
                fatal=True,
            )
        return ActionResult(True, "search", {
            "query": query, "hits": hits, "scanned": scanned, "skipped_unauthorized": skipped,
            "read_bytes": self._read_bytes, "budget_stopped": budget_stop,
        })

    def _read_file(
        self, access: "_SafeAccess", path: Path, parts: tuple[str, ...], *, limit: int,
    ) -> tuple[bytes, "object | None", bool]:
        """通过 broker 的 `_open_regular` 钩子读取文件（内容 + 全文件版本）。

        保留 `_open_regular` 作为唯一打开点：审计与测试可以在这里注入
        "检查之后替换父目录"的竞态钩子，验证父链替换不再有效。
        """
        opened = self._open_regular(path, parts=parts, limit=limit)
        if opened is None:
            raise BrokerError("读取失败：这是二进制文件、符号链接、目录或不可读文件。")
        return opened  # type: ignore[return-value]

    def _do_propose(self, action: Mapping[str, object]) -> ActionResult:
        if not self.can_write:
            raise BrokerFatal(
                f"当前模式是 {self._mode}：不允许提出文件修改；"
                "需要修改请让用户显式使用 --mode edit。"
            )
        raw = action.get("changes")
        if not isinstance(raw, list) or not raw:
            raise BrokerError("propose_changes 需要非空的 changes 列表。")
        if len(raw) > 20:
            raise BrokerError("一次提交最多 20 个文件。")
        applied: list[dict] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise BrokerError("changes 的每一项必须是对象。")
            target = self._resolve(_as_str(item.get("path")), must_exist=False)
            self._authorize(target, write=True)
            if bool(item.get("delete")):
                change = self._change_set.add_delete(target)
            else:
                content = item.get("content")
                if not isinstance(content, str):
                    raise BrokerError("每个变更需要 content 字符串或 delete=true。")
                expected = item.get("expected_sha256")
                if expected is None:
                    expected = self._reads.get(str(target))
                change = self._change_set.add_write(
                    target, content,
                    expected_hash=str(expected) if expected else None,
                )
            applied.append({
                "path": self._rel(target), "kind": change.kind,
                "before_sha256": change.before_hash, "after_sha256": change.after_hash,
                "diff": change.diff_summary,
            })
        return ActionResult(True, "propose_changes", {"staged": applied})

    async def _do_run_command(self, action: Mapping[str, object]) -> ActionResult:
        if self._mode != MODE_EXEC:
            raise BrokerFatal(
                f"当前模式是 {self._mode}：run_command 只在 exec 模式开放；"
                "请让用户显式使用 --mode exec 并在目录 ACL 中包含 exec。"
            )
        if self._exec_policy is None:
            raise BrokerFatal("全局策略未启用 exec（commands.exec 未配置）。")
        raw = action.get("argv")
        if isinstance(raw, str):
            raise BrokerFatal("argv 必须是参数数组（不接受整条 shell 字符串）。")
        if not isinstance(raw, list) or not raw:
            raise BrokerError("run_command 需要非空的 argv 数组。")
        argv = [str(item) for item in raw]
        self._authorize(self._root, write=True)
        cwd = self._resolve(_as_str(action.get("cwd")) or ".")
        if not cwd.is_dir():
            raise BrokerError("cwd 必须是工作目录内的已存在目录。")
        timeout = action.get("timeout")
        before = snapshot_workspace(self._root)
        result = await run_command(
            argv, workspace=self._root, cwd=cwd, policy=self._exec_policy,
            timeout=float(timeout) if isinstance(timeout, (int, float)) else None,
            # R01/R02/T01：受保护路径 + 按当前用户实际能力计算的更具体目录 ACL，
            # 都作为 deny 交给真实沙箱：exec 不能靠"根目录有 exec 授权"绕过内部更严格的授权。
            path_rules=self._exec_deny_paths(),
        )
        after = snapshot_workspace(self._root)
        for row in diff_snapshots(before, after):
            row["command"] = " ".join(argv)
            self._command_changes.append(row)
            self._reads.setdefault(str(self._root / row["path"]), row["after_hash"] or "")
        self._commands.append(result.as_dict())
        if result.denied_reason:
            return ActionResult(
                False, "run_command", error=result.denied_reason, fatal=True
            )
        return ActionResult(True, "run_command", {
            "argv": list(result.argv), "cwd": result.cwd, "exit_code": result.exit_code,
            "stdout": result.stdout[-8000:], "stderr": result.stderr[-4000:],
            "duration_ms": result.duration_ms, "timed_out": result.timed_out,
            "truncated": result.truncated, "sandbox": result.sandbox,
        })

    # ---------- 落盘 ----------

    def _exec_deny_paths(self) -> tuple["ExecPathRule", ...]:
        """exec 沙箱必须限制的路径（R02 + T01）。

        根目录有 exec 授权**不代表**内部每个子目录都能读/写/执行：`workspaces.roots` 里
        更具体（路径更长）的授权项会在 broker 侧覆盖根授权，因此必须按**当前用户在该路径上的
        实际有效能力**（含 `users[user_id]` 覆盖与 admin 集合）生成同样的限制，否则
        `exec` 就成了绕过子目录 ACL 的旁路。

        对每个更具体的授权项计算有效模式，返回 (路径, 允许读, 允许写)：

        - 无 read → 读、写都拒绝（text 授权等价于"这个子树不该被这个用户碰"）；
        - 有 read、无 edit → 拒绝写（只读子树可读不可写）；
        - 有 read + edit → 不加限制（合法写入仍然可用）。

        若某个授权项连沙箱都无法准确表达（例如同一子树内多种权限混排），这里的取舍是
        **收紧**而不是放宽：宁可让该子树在 exec 内不可写，也不允许静默越权。
        """
        rules: list[ExecPathRule] = []
        seen: set[Path] = set()
        # 受保护根交给沙箱的 protected 参数（读写都拒绝）
        for path in self._acl.protected_roots:
            resolved = _real_path(path)
            if resolved not in seen:
                seen.add(resolved)
                rules.append(ExecPathRule(resolved, allow_read=False, allow_write=False))
        for grant in self._acl.grants:
            if grant.path == self._root or not is_within(grant.path, self._root):
                continue
            effective = self._acl.effective_modes_for_path(
                user_id=self._user_id, path=grant.path,
            )
            allow_read = MODE_READ in effective
            allow_write = MODE_EDIT in effective or MODE_EXEC in effective
            if allow_read and allow_write:
                continue
            resolved = _real_path(grant.path)
            if resolved in seen:
                continue
            seen.add(resolved)
            rules.append(ExecPathRule(resolved, allow_read=allow_read, allow_write=allow_write))
        return tuple(rules)

    def commit(self) -> list[FileChange]:
        """执行已登记的变更（调用方必须已持有该目录的锁）。

        提交前逐文件重新授权；授权失败按"该文件失败"记录，而不是把异常抛给调用方
        （否则路径被换掉时会变成未捕获的 fatal，任务与变更状态都会失真）。
        """
        if not self._change_set.changes:
            return []

        def authorize(path: Path) -> None:
            try:
                self._authorize(path, write=True)
            except BrokerFatal as exc:
                raise ChangeError(str(exc)) from None

        return self._change_set.apply(
            authorize=authorize, backup_root=self._backup_root
        )

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._root)) or "."
        except ValueError:
            return str(path)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _ensure_backup_root(value: Path | None) -> Path | None:
    """确保受控备份根存在（任务前补建目录，返回规范路径）。"""
    if value is None:
        return None
    resolved = Path(os.path.realpath(str(value)))
    if not resolved.is_dir():
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 权限/磁盘异常由备份阶段给出明确失败
            return resolved
    return resolved


class _SafeAccess:
    """把 broker 的授权规则接到根 fd 安全打开机制上（R03/R05）。

    - 路径由**组件元组**表示，实际打开从根 fd 逐级 `O_NOFOLLOW` 进行；
    - `allowed()` 在**每个**后代路径打开前重新调用 ACL（最具体授权 + 实时撤权）、
      受保护范围与敏感清单；
    - 遍历只走真实目录，符号链接既不跟随也不作为别名绕过检查。
    """

    def __init__(self, *, broker: FileBroker, safe: SafeRoot) -> None:
        self.broker = broker
        self.safe = safe

    def close(self) -> None:
        self.safe.close()

    def path(self, parts: tuple[str, ...]) -> Path:
        return self.safe.lexical(parts)

    def allowed(self, parts: tuple[str, ...], *, write: bool) -> bool:
        """逐项复查授权；失败返回 False（调用方跳过该项，不中断整个动作）。"""
        try:
            self.broker._authorize(self.path(parts), write=write)
        except (BrokerFatal, BrokerError):
            return False
        return True

    def iter_files(
        self, base: tuple[str, ...], *, max_entries: int | None = None
    ) -> Iterator[Path]:
        """有界枚举 base 之下的普通文件（不跟随符号链接、逐项复查授权）。"""
        budget = self.broker._budget
        ceiling = budget.max_search_entries if max_entries is None else max_entries
        seen = 0
        stack: list[tuple[str, ...]] = [base]
        while stack:
            current = stack.pop()
            try:
                entries = self.safe.scandir(current)
            except OSError:
                continue
            for entry in entries:
                if seen >= ceiling:
                    self.broker._budget_exhausted = True
                    return
                seen += 1
                child = (*current, entry.name)
                try:
                    # os.scandir(fd) 的 DirEntry 不能按路径 stat：统一走安全 lstat
                    info = self.safe.lstat(child)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue  # 不以符号链接别名绕过敏感/范围检查
                if stat.S_ISDIR(info.st_mode):
                    if self.allowed(child, write=False):
                        stack.append(child)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if not self.allowed(child, write=False):
                    continue
                yield self.path(child)


def parse_model_actions(text: str) -> tuple[list[dict], str]:
    """从模型回复中提取结构化动作。

    只接受一个 JSON 对象：``{"actions": [...]}`` 或 ``{"final": "..."}``。
    坏 JSON、未知结构一律抛错——绝不执行模型返回的自由文本或代码。
    """
    raw = (text or "").strip()
    if not raw:
        raise BrokerError("模型没有返回任何动作或最终答复。")
    if raw.startswith("```"):
        body = raw.strip("`")
        if body.startswith("json"):
            body = body[4:]
        raw = body.strip()
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BrokerError(
            "模型返回的不是合法 JSON 动作；本模式要求 {\"actions\": [...]} 或 {\"final\": \"...\"}。"
        ) from None
    if not isinstance(payload, dict):
        raise BrokerError("模型返回的 JSON 必须是对象。")
    if "final" in payload:
        return [], str(payload.get("final") or "")
    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise BrokerError("模型返回的 JSON 缺少 actions 列表。")
    clean: list[dict] = []
    for item in actions:
        if not isinstance(item, dict):
            raise BrokerError("actions 的每一项必须是对象。")
        clean.append(dict(item))
    return clean, ""


def text_result_hash(text: str) -> str:
    return text_hash(text)


def is_text_mode(mode: str) -> bool:
    return mode in {MODE_TEXT, MODE_READ}
