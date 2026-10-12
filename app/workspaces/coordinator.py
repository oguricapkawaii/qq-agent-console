"""全入口目录协调器：同目录/父子目录任务整段互斥，无关目录可并行。

覆盖范围（统一架构第 4 节）：单任务、广播子任务、team 步骤、公共文件写入
以及原生 harness 调用全部走同一协调器，不能只锁 team。

首版语义：冲突目录任务整段独占锁（含 read）。不做读写锁优化——这是保守且
可验证的选择；`/team plan` 会提示需要 --after 的串行化影响。

锁的释放覆盖正常返回、异常与取消（contextmanager + finally）。
多 worker 说明：进程内锁只在一个进程内有效。生产配置若声明多个执行 worker，
必须同时声明跨进程锁实现，否则启动即拒绝（见 `enforce_worker_policy`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.security.paths import overlap

logger = logging.getLogger("qqbot.workspaces.coordinator")


class WorkspaceBusy(RuntimeError):
    """获取目录锁超时。"""

    def __init__(self, message: str, *, holders: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.holders = holders


@dataclass(slots=True)
class LockRecord:
    key: Path
    label: str
    acquired_at: float
    task_id: str = ""


@dataclass(slots=True)
class _State:
    held: dict[str, LockRecord] = field(default_factory=dict)
    condition: asyncio.Condition | None = None


class WorkspaceCoordinator:
    """进程内目录互斥锁（见模块文档的多 worker 限制）。"""

    def __init__(self, *, acquire_timeout: float = 30.0, max_workers: int = 1) -> None:
        self._timeout = max(0.0, float(acquire_timeout))
        self._max_workers = max(1, int(max_workers))
        self._state = _State()
        self._waiting = 0

    # ---------- 只读查询 ----------

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def waiting(self) -> int:
        return self._waiting

    def active_locks(self) -> list[dict]:
        return [
            {
                "path": str(record.key),
                "label": record.label,
                "task_id": record.task_id,
                "held_ms": int((time.monotonic() - record.acquired_at) * 1000),
            }
            for record in self._state.held.values()
        ]

    def conflicts(self, paths: Iterable[Path]) -> list[str]:
        holders: list[str] = []
        for path in paths:
            for record in self._state.held.values():
                if overlap(path, record.key):
                    holders.append(f"{record.key}（{record.label}）")
        return holders

    # ---------- 获取 / 释放 ----------

    async def _condition(self) -> asyncio.Condition:
        if self._state.condition is None:
            self._state.condition = asyncio.Condition()
        return self._state.condition

    @contextlib.asynccontextmanager
    async def lock(
        self, paths: Iterable[Path], *, label: str, task_id: str = "",
        timeout: float | None = None,
    ):
        """获取一组目录的独占锁；冲突时等待，超时抛 WorkspaceBusy。"""
        canonical = sorted({Path(path) for path in paths}, key=str)
        if not canonical:
            yield ()
            return
        deadline = time.monotonic() + (self._timeout if timeout is None else max(0.0, timeout))
        condition = await self._condition()
        acquired: list[str] = []
        self._waiting += 1
        try:
            async with condition:
                while True:
                    holders = self.conflicts(canonical)
                    if not holders:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkspaceBusy(
                            "目录正被其他任务占用，请稍后重试：" + "；".join(sorted(set(holders))),
                            holders=tuple(sorted(set(holders))),
                        )
                    try:
                        await asyncio.wait_for(condition.wait(), timeout=min(remaining, 1.0))
                    except asyncio.TimeoutError:
                        continue
                for path in canonical:
                    key = str(path)
                    self._state.held[key] = LockRecord(
                        key=path, label=label, acquired_at=time.monotonic(), task_id=task_id
                    )
                    acquired.append(key)
                logger.info(
                    "workspace_lock acquired label=%s task=%s paths=%s",
                    label, task_id or "-", ",".join(acquired),
                )
            try:
                yield tuple(canonical)
            finally:
                async with condition:
                    for key in acquired:
                        self._state.held.pop(key, None)
                    condition.notify_all()
                logger.info(
                    "workspace_lock released label=%s task=%s paths=%s",
                    label, task_id or "-", ",".join(acquired),
                )
        finally:
            self._waiting -= 1
            # 异常路径（含取消）也必须唤醒等待者
            if not acquired:
                async with condition:
                    condition.notify_all()


def enforce_worker_policy(*, max_workers: int, cross_process_lock: bool, source: str) -> None:
    """F15：跨进程锁尚未实现，因此**无条件**限制单执行 worker。

    不接受 `cross_process_lock: true` 之类布尔声明作为绕道：只有当真的注入了可验证的
    跨进程锁实现后，才允许放宽（届时把实现注册进本模块并改写本函数）。
    """
    if max_workers > 1:
        raise ValueError(
            f"{source} 声明了 {max_workers} 个执行 worker，但本项目当前只有进程内目录锁，"
            "跨进程锁尚未实现；请把 worker 数设为 1。"
            "（布尔开关 cross_process_lock 不能替代实现，因此它不再被接受。）"
        )
    if cross_process_lock:
        raise ValueError(
            f"{source} 设置了 cross_process_lock=true，但跨进程锁实现并不存在；"
            "该开关已被拒绝，避免以声明冒充能力。"
        )
