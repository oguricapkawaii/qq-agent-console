"""协作任务调度器：DAG ready 队列、全局并发、失败传播、取消与超时。

- 每个团队任务用一个受监督的 asyncio.Task，强引用集合 + done_callback 统一收异常；
- 失败只阻塞其下游，无关分支继续；只有全部步骤成功才判团队成功；
- cancel 传播到 Task，晚到结果因条件 UPDATE 落空而丢弃；
- 不自动重试任何模型调用；retry 由用户显式发起并复用成功结果。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque

from app.config import Settings
from app.workflows.executor import StepContext, StepExecutor
from app.workflows.models import (
    ExecutionOutcome,
    PlanSnapshot,
    StepStatus,
    TeamStatus,
    UpstreamResult,
    dependents_map,
    downstream_closure,
    topological_layers,
)
from app.workflows.notifier import Notifier
from app.workflows.store import WorkflowStore, snapshot_from_json

logger = logging.getLogger("qqbot.workflows.runner")

_ERROR_TEXT = {
    "timeout": "超时",
    "network": "网络错误",
    "parse": "响应解析失败",
    "harness_failed": "本机 Harness 调用失败",
    "harness_approval_required": "本机 Harness 需要人工交互",
    "selection_mismatch": "返回的模型或强度与选择不一致",
    "output_limit": "输出超过大小限制",
    "input_limit": "上游输入超过大小限制",
    "codex_failed": "Codex 调用失败",
    "tool_used": "文本步骤出现了工具调用，已按失败处理",
    "empty": "模型返回空内容",
    "internal": "内部错误（详情见服务端日志）",
    "unauthorized": "授权已撤销",
    "unknown_agent": "Agent 不存在",
    "agent_disabled": "Agent 已禁用",
    "not_configured": "Agent 未配置",
    "invalid_option": "模型或强度不可用",
    "workflow_timeout": "团队总时限已到",
    "invalid_plan": "计划依赖非法",
    # 统一控制台新增类别
    "broker_denied": "文件代理拒绝执行（权限/越界/预算）",
    "bad_actions": "模型未按要求返回结构化动作",
    "budget_exceeded": "动作预算耗尽",
    "capability_not_supported": "后端不支持该模式",
    "workspace_busy": "目标目录被其他任务占用",
    "rate_limited": "请求过于频繁",
    "empty": "模型返回空内容",
    "access_revoked": "授权已撤销",
    # V01：会话完成但没有任何实际执行的操作
    "no_operation": "本步没有任何实际执行的操作（未修改、未执行）",
}


def error_text(kind: str | None) -> str:
    if not kind:
        return "未知错误"
    if kind.startswith("blocked_by:"):
        return f"上游失败：{kind.removeprefix('blocked_by:')}"
    if kind.startswith("api_error_"):
        return f"API 错误（HTTP {kind.removeprefix('api_error_')}）"
    return _ERROR_TEXT.get(kind, f"未知错误（{kind}）")


class WorkflowRunner:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        executor: StepExecutor,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self._store = store
        self._executor = executor
        self._notifier = notifier
        self._settings = settings
        self._tasks: dict[str, asyncio.Task] = {}
        self._step_slots = asyncio.Semaphore(max(1, settings.team_max_parallel_steps))
        self._closing = False

    # ---------- 生命周期 ----------

    @property
    def active_teams(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    async def start_team(self, team_id: str, round_no: int) -> bool:
        """把一个已冻结轮次排入后台执行；同一团队不重复启动。"""
        if self._closing:
            return False
        existing = self._tasks.get(team_id)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(self._run_team(team_id, round_no), name=f"team-{team_id}")
        self._tasks[team_id] = task
        task.add_done_callback(lambda finished, key=team_id: self._on_done(key, finished))
        return True

    def _on_done(self, team_id: str, task: asyncio.Task) -> None:
        # 只有当前登记的任务才能移除自己：旧轮次的回调不能误删新轮次的任务
        if self._tasks.get(team_id) is task:
            self._tasks.pop(team_id, None)
        if task.cancelled():
            logger.info("team=%s 后台任务已取消", team_id)
            return
        exc = task.exception()
        if exc is not None:
            logger.error("team=%s 后台任务异常：%r", team_id, exc)

    async def cancel_team(self, team_id: str) -> bool:
        """取消活动团队；无论任务是否已经启动，都保证 DB 收敛到 cancelled。"""
        task = self._tasks.get(team_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        workflow = self._store.get_workflow(team_id)
        if workflow is None:
            return False
        if workflow.status in TeamStatus.ACTIVE and workflow.round > 0:
            # 任务在启动前就被取消时协程体不会执行，这里补一次条件更新
            self._store.cancel_round(team_id, workflow.round)
            return True
        return task is not None

    async def shutdown(self, grace: float = 5.0) -> None:
        """先停止接受新任务，再取消全部任务并等待收敛。

        与用户 cancel 不同：关闭把未完成任务标记为 interrupted（本机中止），
        用户重启后可显式 /team retry 恢复；绝不自动重新调用模型。
        """
        self._closing = True
        # 先记下团队 ID：任务取消后 done_callback 会把它们从 _tasks 移除
        team_ids = list(self._tasks)
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=grace)
        for team_id in team_ids:
            workflow = self._store.get_workflow(team_id)
            if workflow is None or workflow.status not in TeamStatus.ACTIVE or workflow.round == 0:
                continue
            self._store.interrupt_round(team_id, workflow.round)
        self._tasks.clear()

    # ---------- 轮次执行 ----------

    async def _run_team(self, team_id: str, round_no: int) -> None:
        run = self._store.get_run(team_id, round_no)
        if run is None:
            logger.warning("team=%s round=%d 轮次不存在", team_id, round_no)
            return
        try:
            snapshot = snapshot_from_json(run.plan_json)
        except (ValueError, KeyError, TypeError):
            # 计划快照损坏：整轮失败，不把团队留在 queued
            logger.exception("team=%s round=%d 计划快照无法解析", team_id, round_no)
            self._store.fail_round(team_id, round_no, "invalid_plan")
            await self._notify(
                team_id, f"❌ 团队任务 {team_id} 的计划快照损坏，已停止；请新建计划。"
            )
            return
        if not self._store.set_run_running(team_id, round_no):
            logger.info("team=%s round=%d 已不是 queued，跳过执行", team_id, round_no)
            return
        try:
            final = await self._execute_round(team_id, round_no, snapshot)
        except asyncio.CancelledError:
            # 用户取消 -> cancelled；服务关闭 -> interrupted（可显式 retry）
            if self._closing:
                self._store.interrupt_round(team_id, round_no)
            else:
                self._store.cancel_round(team_id, round_no)
            raise
        except Exception:
            # 非取消异常也必须收敛状态，绝不把团队留在 running
            logger.exception("team=%s round=%d 执行异常，已整轮失败", team_id, round_no)
            self._store.fail_round(team_id, round_no, "internal")
            await self._notify(
                team_id,
                f"❌ 团队任务 {team_id} 内部错误，已停止；"
                f"可用 /team result 查看已完成步骤，或 /team retry 重试。",
            )
            raise
        if final == TeamStatus.SUCCEEDED:
            await self._notify(team_id, f"🎉 团队任务 {team_id} 已完成：全部 {len(snapshot.steps)} 步成功。")
        elif final == TeamStatus.FAILED:
            await self._notify(
                team_id,
                f"❌ 团队任务 {team_id} 失败。\n可用 /team result {team_id} 查看成功步骤的结果，"
                f"修复后 /team retry {team_id} 只重跑失败与受阻步骤。",
            )

    async def _execute_round(self, team_id: str, round_no: int, snapshot: PlanSnapshot) -> str:
        step_ids = [step.step_id for step in snapshot.steps]
        dependencies = {step.step_id: set(step.depends_on) for step in snapshot.steps}
        if topological_layers(step_ids, dependencies) is None:
            self._store.fail_round(team_id, round_no, "invalid_plan")
            logger.error("team=%s round=%d 计划依赖非法，已整轮失败", team_id, round_no)
            return TeamStatus.FAILED
        children = dependents_map(step_ids, dependencies)
        finished: dict[str, str] = {}
        for step_id, status in self._store.step_statuses(team_id, round_no).items():
            if status in StepStatus.TERMINAL:
                finished[step_id] = status
        ready: deque[str] = deque()
        for step_id in step_ids:
            if step_id in finished:
                continue
            statuses = [finished.get(dep) for dep in dependencies[step_id]]
            if any(status is not None and status != StepStatus.SUCCEEDED for status in statuses):
                self._store.mark_step_blocked(team_id, round_no, step_id, "blocked_by:previous")
                finished[step_id] = StepStatus.BLOCKED
            elif all(status == StepStatus.SUCCEEDED for status in statuses):
                ready.append(step_id)

        concurrency = max(1, self._settings.team_max_parallel_steps)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.team_workflow_timeout_seconds
        running: dict[asyncio.Task, str] = {}
        timed_out = False
        try:
            while ready or running:
                while ready and len(running) < concurrency:
                    step_id = ready.popleft()
                    if not self._store.claim_step(team_id, round_no, step_id):
                        record = self._store.get_step_run(team_id, round_no, step_id)
                        finished[step_id] = record.status if record else StepStatus.CANCELLED
                        continue
                    task = asyncio.create_task(
                        self._execute_step(team_id, round_no, snapshot, step_id),
                        name=f"team-{team_id}-{step_id}",
                    )
                    running[task] = step_id
                if not running:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                done, _ = await asyncio.wait(
                    set(running), return_when=asyncio.FIRST_COMPLETED, timeout=remaining
                )
                if not done:
                    timed_out = True
                    break
                for task in done:
                    step_id = running.pop(task)
                    status = task.result()
                    finished[step_id] = status
                    if status == StepStatus.SUCCEEDED:
                        for child in children.get(step_id, []):
                            if child in finished or child in ready:
                                continue
                            if all(finished.get(dep) == StepStatus.SUCCEEDED
                                   for dep in dependencies[child]):
                                ready.append(child)
                    else:
                        for downstream in downstream_closure(step_ids, dependencies, step_id):
                            if downstream in finished:
                                continue
                            if self._store.mark_step_blocked(
                                team_id, round_no, downstream, f"blocked_by:{step_id}"
                            ):
                                finished[downstream] = StepStatus.BLOCKED
                            else:
                                record = self._store.get_step_run(team_id, round_no, downstream)
                                finished[downstream] = (
                                    record.status if record else StepStatus.CANCELLED
                                )
                            with contextlib.suppress(ValueError):
                                ready.remove(downstream)
            if timed_out:
                await self._cancel_tasks(running)
                self._store.fail_round(team_id, round_no, "workflow_timeout")
                await self._notify(
                    team_id,
                    f"⏱ 团队任务 {team_id} 超过总时限 "
                    f"{int(self._settings.team_workflow_timeout_seconds)} 秒，已停止未完成步骤。",
                )
                return TeamStatus.FAILED
            return self._store.complete_round(team_id, round_no)
        finally:
            await self._cancel_tasks(running)

    async def _cancel_tasks(self, running: dict[asyncio.Task, str]) -> None:
        if not running:
            return
        tasks = list(running)
        running.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_step(
        self, team_id: str, round_no: int, snapshot: PlanSnapshot, step_id: str
    ) -> str:
        step = next(item for item in snapshot.steps if item.step_id == step_id)
        workflow = self._store.get_workflow(team_id)
        group_id = workflow.group_id if workflow else 0
        owner_user_id = workflow.owner_user_id if workflow else 0
        upstream: list[UpstreamResult] = []
        for dep in step.depends_on:
            record = self._store.get_step_run(team_id, round_no, dep)
            if record is None or record.status != StepStatus.SUCCEEDED:
                continue
            upstream.append(
                UpstreamResult(step_id=dep, agent=self._agent_name(snapshot, dep),
                               answer=record.answer, status=record.status)
            )
        upstream.sort(key=lambda item: self._step_seq(snapshot, item.step_id))
        context = StepContext(
            team_id=team_id, round=round_no, group_id=group_id,
            owner_user_id=owner_user_id, goal=snapshot.goal, step=step,
            upstream=tuple(upstream),
        )
        await self._notify_step(
            group_id, owner_user_id,
            f"▶️ [{team_id}/{step_id}] {step.agent} 开始 · 后端 {step.backend} · "
            f"模型 {step.choice.model} · 强度 {step.choice.effort or '模型默认'} · "
            f"权限 {step.mode}"
            + (f" · 目录 {step.workspace}" if step.workspace else ""),
        )
        try:
            async with self._step_slots:
                outcome = await self._executor.execute(context)
        except asyncio.CancelledError:
            # 取消不改状态：状态由 _run_team 的取消路径统一落库
            raise
        except Exception:
            logger.exception("team=%s step=%s 执行异常", team_id, step_id)
            outcome = ExecutionOutcome(
                ok=False, status=StepStatus.FAILED, error="internal",
                model=step.choice.model, effort=step.choice.effort,
            )
        stored = self._store.finish_step(
            team_id=team_id, round_no=round_no, step_id=step_id,
            status=outcome.status if outcome.ok else StepStatus.FAILED,
            answer=outcome.answer, error=outcome.error, duration_ms=outcome.duration_ms,
            task_id=outcome.task_id, model=outcome.model, effort=outcome.effort,
            change_fingerprint=outcome.change_fingerprint,
            changed_files=outcome.changed_files, commands=outcome.commands,
            detail=outcome.detail,
            input_fingerprint=outcome.input_fingerprint,
        )
        if not stored:
            record = self._store.get_step_run(team_id, round_no, step_id)
            return record.status if record else StepStatus.CANCELLED
        if outcome.ok:
            changed = f" · 变更 {outcome.changed_files} 个文件" if outcome.changed_files else ""
            await self._notify_step(
                group_id, owner_user_id,
                f"✅ [{team_id}/{step_id}] 完成（{outcome.duration_ms / 1000:.1f} 秒{changed}）",
            )
            return StepStatus.SUCCEEDED
        if outcome.detail:
            logger.warning(
                "team=%s round=%d step=%s 失败 error=%s detail=%s",
                team_id, round_no, step_id, outcome.error, outcome.detail,
            )
        extra = f"（{outcome.detail}）" if outcome.detail else ""
        side = ""
        if outcome.changed_files:
            side = f"；已部分写入 {outcome.changed_files} 个文件，取消/重跑前请先核对"
        await self._notify_step(
            group_id, owner_user_id,
            f"❌ [{team_id}/{step_id}] 失败：{error_text(outcome.error)}{extra}{side}",
        )
        return StepStatus.FAILED

    @staticmethod
    def _agent_name(snapshot: PlanSnapshot, step_id: str) -> str:
        for step in snapshot.steps:
            if step.step_id == step_id:
                return step.agent
        return "?"

    @staticmethod
    def _step_seq(snapshot: PlanSnapshot, step_id: str) -> int:
        for step in snapshot.steps:
            if step.step_id == step_id:
                return step.seq
        return 0

    # ---------- 通知 ----------

    async def _notify(self, team_id: str, text: str) -> None:
        workflow = self._store.get_workflow(team_id)
        if workflow is None:
            return
        await self._notifier.notify(workflow.group_id, workflow.owner_user_id, f"[{team_id}] {text}")

    async def _notify_step(self, group_id: int, owner_user_id: int, text: str) -> None:
        await self._notifier.notify(group_id, owner_user_id, text)
