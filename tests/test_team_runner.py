"""WorkflowRunner 行为测试：DAG 分层、并行重叠、并发上限、失败传播、
retry 复用、cancel 语义、超时、重启中断、通知失败与目录锁。
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from app.commands.options import MODE_READ, MODE_TEXT
from app.security.paths import canonical
from app.transports.base import TransportError
from app.workflows.models import (
    FrozenChoice,
    FrozenStep,
    PlanSnapshot,
    StepStatus,
    TeamStatus,
)
from helpers import (
    FakeAgent,
    RecordingTransport,
    TeamHarness,
    make_team_settings,
    workspace_settings,
)

OTHER_USER = 556


class ConcurrencyProbe:
    """记录并发峰值与当前并发的假钩子。"""

    def __init__(self, hold: float = 0.05) -> None:
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.hold = hold
        self.completed: list[str] = []

    async def hook(self, agent, messages) -> None:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.hold)
        finally:
            self.active -= 1
        self.completed.append(agent.name)


class FailingTransport(RecordingTransport):
    """本文件局部助手：所有外发都失败的传输（模拟 QQ 侧不可达）。"""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 0

    async def send_group_message(self, group_id: int, text: str) -> None:
        self.failures += 1
        raise TransportError("boom")


async def test_independent_steps_overlap(tmp_path):
    probe = ConcurrencyProbe()
    settings = make_team_settings(tmp_path, team_max_parallel_steps=2)
    agents = {
        "glm": FakeAgent("glm", reply="A", hook=probe.hook),
        "deepseek": FakeAgent("deepseek", reply="B", hook=probe.hook),
    }
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 独立A")
        await harness.command(f"/team add {team_id} b deepseek -- 独立B")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        assert probe.peak == 2, "两个无依赖步骤必须真正并行"
    finally:
        await harness.aclose()


async def test_dependency_layer_starts_after_whole_layer_finishes(tmp_path):
    probe = ConcurrencyProbe(hold=0.05)
    settings = make_team_settings(tmp_path, team_max_parallel_steps=2)
    agents = {
        "glm": FakeAgent("glm", reply="A", hook=probe.hook),
        "deepseek": FakeAgent("deepseek", reply="B", hook=probe.hook),
        "gpt": FakeAgent("gpt", reply="C", hook=probe.hook),
    }
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 层1")
        await harness.command(f"/team add {team_id} b deepseek -- 层1")
        await harness.command(f"/team add {team_id} c gpt --after a,b -- 层2")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        assert probe.peak == 2
        # 汇总步骤必须在两个上游都完成之后才开始
        assert probe.completed[-1] == "gpt"
        assert set(probe.completed[:2]) == {"glm", "deepseek"}
    finally:
        await harness.aclose()


async def test_global_parallel_limit_across_teams(tmp_path):
    probe = ConcurrencyProbe(hold=0.08)
    settings = make_team_settings(
        tmp_path, team_max_parallel_steps=2, team_max_active_per_user=2,
        team_max_active_global=4,
    )
    agents = {
        "glm": FakeAgent("glm", reply="A", hook=probe.hook),
        "deepseek": FakeAgent("deepseek", reply="B", hook=probe.hook),
    }
    harness = TeamHarness(settings, agents)
    try:
        teams = []
        for index in range(2):
            created = await harness.command("/team create 目标")
            team_id = created.split()[2]
            await harness.command(f"/team add {team_id} a{index} glm -- 独立A")
            await harness.command(f"/team add {team_id} b{index} deepseek -- 独立B")
            teams.append(team_id)
        for team_id in teams:
            await harness.command(f"/team run {team_id}")
        for team_id in teams:
            assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        assert probe.peak == 2, "全局步骤并发上限必须生效"
    finally:
        await harness.aclose()


async def test_same_directory_steps_hold_the_shared_lock(tmp_path):
    """文件步骤在整段执行期间持有「群 × 目录」公共锁，结束后释放。"""
    root = tmp_path / "proj"
    root.mkdir()
    expected = str(canonical(str(root)))
    seen_locks: list[list[dict]] = []
    counter = {"n": 0}

    def reply(agent, messages):
        counter["n"] += 1
        if counter["n"] % 2 == 1:
            return json.dumps({"actions": [{"action": "list", "path": "."}]})
        return json.dumps({"final": "done"})

    async def hook(agent, messages):
        seen_locks.append(harness.coordinator.active_locks())

    worker = FakeAgent("worker", reply=reply, hook=hook)
    harness = TeamHarness(workspace_settings(tmp_path, root=root), {"worker": worker})
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "a", "worker", mode=MODE_READ, workspace=str(root))
        harness.add_step(team_id, "b", "worker", mode=MODE_READ, workspace=str(root))
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        assert seen_locks, "文件步骤必须真的调用模型"
        for locks in seen_locks:
            assert any(lock["path"] == expected for lock in locks), locks
        assert harness.coordinator.active_locks() == []  # 结束后不残留锁
        # 两个步骤都要执行（锁只保证互斥，不代替业务依赖）
        assert counter["n"] == 4
    finally:
        await harness.aclose()


async def test_failure_blocks_only_downstream_and_retry_reuses_success(tmp_path):
    call_count = {"glm": 0}

    async def flaky_hook(agent, messages) -> None:
        call_count["glm"] += 1
        agent._error = "network" if call_count["glm"] == 1 else None

    settings = make_team_settings(tmp_path, team_max_parallel_steps=2)
    agents = {
        "glm": FakeAgent("glm", reply="A-成功", error="network", hook=flaky_hook),
        "deepseek": FakeAgent("deepseek", reply="C-成功"),
        "gpt": FakeAgent("gpt", reply="B-成功"),
    }
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 会失败一次")
        await harness.command(f"/team add {team_id} b gpt --after a -- 依赖失败者")
        await harness.command(f"/team add {team_id} c deepseek -- 无关分支")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.FAILED
        runs = harness.workflow_store.list_step_runs(team_id, 1)
        assert runs["a"].status == StepStatus.FAILED
        assert runs["b"].status == StepStatus.BLOCKED
        assert runs["c"].status == StepStatus.SUCCEEDED  # 无关分支继续
        assert harness.agents["gpt"].calls == []  # 下游未被调用
        # 团队失败时成功结果仍可查询
        result = await harness.command(f"/team result {team_id}")
        assert "C-成功" in result and "失败" in result

        # retry 只重跑失败与受阻步骤，复用 c
        retry_text = await harness.command(f"/team retry {team_id}")
        assert "第 2 轮" in retry_text and "c" in retry_text
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        assert call_count["glm"] == 2
        assert len(harness.agents["deepseek"].calls) == 1  # c 复用，未重跑
        assert len(harness.agents["gpt"].calls) == 1
        round2 = harness.workflow_store.list_step_runs(team_id, 2)
        assert round2["c"].status == StepStatus.SUCCEEDED
        assert round2["c"].source_round == 1
    finally:
        await harness.aclose()


async def test_cancel_running_team_and_late_result_ignored(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_hook(agent, messages) -> None:
        started.set()
        await release.wait()

    settings = make_team_settings(tmp_path, team_max_parallel_steps=1)
    agents = {"glm": FakeAgent("glm", reply="晚到", hook=slow_hook)}
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 慢步骤")
        await harness.command(f"/team add {team_id} b glm --after a -- 后续")
        await harness.command(f"/team run {team_id}")
        await asyncio.wait_for(started.wait(), timeout=3)
        text = await harness.command(f"/team cancel {team_id}")
        release.set()
        assert "已取消" in text
        await asyncio.sleep(0.05)
        workflow = harness.workflow_store.get_workflow(team_id)
        assert workflow.status == TeamStatus.CANCELLED
        runs = harness.workflow_store.list_step_runs(team_id, 1)
        assert runs["a"].status == StepStatus.CANCELLED
        assert runs["b"].status == StepStatus.CANCELLED
        # 晚到结果不能翻转取消状态
        assert harness.workflow_store.finish_step(
            team_id=team_id, round_no=1, step_id="a",
            status=StepStatus.SUCCEEDED, answer="迟到",
        ) is False
        assert harness.workflow_store.get_step_run(team_id, 1, "a").status == StepStatus.CANCELLED
        assert harness.runner.active_teams == ()
    finally:
        release.set()
        await harness.aclose()


async def test_cancel_queued_team(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_hook(agent, messages) -> None:
        started.set()
        await release.wait()

    settings = make_team_settings(tmp_path, team_max_parallel_steps=1)
    agents = {"glm": FakeAgent("glm", reply="x", hook=slow_hook)}
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 步骤a")
        await harness.command(f"/team add {team_id} b glm --after a -- 步骤b")
        await harness.command(f"/team add {team_id} c glm --after b -- 步骤c")
        await harness.command(f"/team run {team_id}")
        await asyncio.wait_for(started.wait(), timeout=3)
        await harness.command(f"/team cancel {team_id}")
        release.set()
        await asyncio.sleep(0.05)
        runs = harness.workflow_store.list_step_runs(team_id, 1)
        assert {record.status for record in runs.values()} == {StepStatus.CANCELLED}
    finally:
        release.set()
        await harness.aclose()


async def test_workflow_timeout_stops_team(tmp_path):
    async def hanging_hook(agent, messages) -> None:
        await asyncio.sleep(30)

    settings = make_team_settings(
        tmp_path, team_workflow_timeout_seconds=0.2, team_step_timeout_seconds=10.0
    )
    agents = {"glm": FakeAgent("glm", reply="x", hook=hanging_hook)}
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 慢步骤")
        await harness.command(f"/team add {team_id} b glm --after a -- 后续")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.FAILED
        runs = harness.workflow_store.list_step_runs(team_id, 1)
        assert runs["a"].status == StepStatus.FAILED
        assert runs["a"].error == "workflow_timeout"
        assert harness.runner.active_teams == ()
        assert any("总时限" in text for text in harness.transport.texts())
    finally:
        await harness.aclose()


async def test_step_timeout(tmp_path):
    async def hanging_hook(agent, messages) -> None:
        await asyncio.sleep(30)

    settings = make_team_settings(
        tmp_path, team_step_timeout_seconds=0.1, team_workflow_timeout_seconds=10.0
    )
    agents = {"glm": FakeAgent("glm", reply="x", hook=hanging_hook)}
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 慢步骤")
        await harness.command(f"/team run {team_id}")
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.FAILED
        record = harness.workflow_store.get_step_run(team_id, 1, "a")
        assert record.status == StepStatus.FAILED and record.error == "timeout"
    finally:
        await harness.aclose()


async def test_shutdown_interrupts_active_runs_for_retry(tmp_path):
    started = asyncio.Event()

    async def hanging_hook(agent, messages) -> None:
        started.set()
        await asyncio.sleep(30)

    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm", reply="x", hook=hanging_hook)}
    harness = TeamHarness(settings, agents)
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 慢步骤")
        await harness.command(f"/team run {team_id}")
        await asyncio.wait_for(started.wait(), timeout=3)
        await harness.runner.shutdown()
        assert harness.runner.active_teams == ()
        # 关闭≠用户取消：标记 interrupted 以便重启后显式 retry
        assert harness.workflow_store.get_workflow(team_id).status == TeamStatus.INTERRUPTED
        assert harness.workflow_store.get_step_run(
            team_id, 1, "a"
        ).status == StepStatus.INTERRUPTED
    finally:
        await harness.aclose()


async def test_background_task_exceptions_are_supervised(tmp_path):
    """后台任务异常必须被 done_callback 统一收集，不进入事件循环的未处理异常。"""
    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm", reply="x")}
    harness = TeamHarness(settings, agents)
    loop = asyncio.get_running_loop()
    captured: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    try:
        def boom(*args, **kwargs):
            raise RuntimeError("store exploded")

        harness.workflow_store.get_run = boom  # type: ignore[method-assign]
        assert await harness.runner.start_team("t00000000", 1) is True
        await asyncio.sleep(0.05)
        assert harness.runner.active_teams == ()
        assert captured == [], f"未处理的后台异常：{captured}"
    finally:
        loop.set_exception_handler(previous)
        await harness.aclose()


async def test_internal_error_marks_round_failed(tmp_path):
    """非取消异常必须把团队收敛为 failed，不能留在 running。"""
    settings = make_team_settings(tmp_path)
    harness = TeamHarness(settings, {"glm": FakeAgent("glm", reply="x")})
    try:
        created = await harness.command("/team create 目标")
        team_id = created.split()[2]
        await harness.command(f"/team add {team_id} a glm -- 步骤")

        async def boom(*args, **kwargs):
            raise RuntimeError("runner exploded")

        harness.runner._execute_round = boom  # type: ignore[method-assign]
        await harness.command(f"/team run {team_id}")
        await asyncio.sleep(0.05)
        assert harness.runner.active_teams == ()
        assert harness.workflow_store.get_workflow(team_id).status == TeamStatus.FAILED
        assert harness.workflow_store.get_step_run(team_id, 1, "a").status == StepStatus.FAILED
        assert any("内部错误" in text for text in harness.transport.texts())
    finally:
        await harness.aclose()


async def test_invalid_plan_snapshot_fails_round(tmp_path):
    """计划快照损坏：整轮失败并通知，不把团队留在 queued。"""
    settings = make_team_settings(tmp_path)
    harness = TeamHarness(settings, {"glm": FakeAgent("glm", reply="x")})
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "a", "glm")
        harness.workflow_store.freeze_run(team_id, _snapshot(team_id, harness))
        with harness.workflow_store._conn:
            harness.workflow_store._conn.execute(
                "UPDATE team_runs SET plan_json=? WHERE team_id=? AND round=1",
                ("{ 这不是 JSON", team_id),
            )
        assert await harness.runner.start_team(team_id, 1) is True
        assert await harness.wait_team(team_id) == TeamStatus.FAILED
        assert harness.workflow_store.get_run(team_id, 1).error == "invalid_plan"
        assert harness.agents["glm"].calls == []
        assert any("快照损坏" in text for text in harness.transport.texts())
    finally:
        await harness.aclose()


async def test_stale_done_callback_keeps_new_round_task(tmp_path):
    """旧轮次的 done_callback 不能移除新轮次登记的任务。"""
    settings = make_team_settings(tmp_path)
    harness = TeamHarness(settings, {"glm": FakeAgent("glm")})
    runner = harness.runner
    try:
        async def quick():
            return None

        async def sleeper():
            await asyncio.sleep(30)

        old = asyncio.create_task(quick())
        await old
        new = asyncio.create_task(sleeper())
        runner._tasks["t00000001"] = new
        runner._on_done("t00000001", old)  # 旧轮次回调
        assert runner._tasks.get("t00000001") is new
        new.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await new
        runner._on_done("t00000001", new)
        assert "t00000001" not in runner._tasks
    finally:
        await harness.aclose()


async def test_shutdown_interrupts_never_started_round(tmp_path):
    """shutdown 时首次尚未进入的 queued 任务也必须落库 interrupted。"""
    settings = make_team_settings(tmp_path)
    agents = {"glm": FakeAgent("glm", reply="x")}
    harness = TeamHarness(settings, agents)
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "a", "glm")
        snapshot = PlanSnapshot(
            team_id=team_id, goal="目标",
            steps=(FrozenStep(step_id="a", seq=0, agent="glm", instruction="步骤",
                              depends_on=(), choice=FrozenChoice(model="fake-glm")),),
        )
        assert harness.workflow_store.freeze_run(team_id, snapshot) == 1
        assert await harness.runner.start_team(team_id, 1) is True
        await harness.runner.shutdown()
        assert harness.workflow_store.get_workflow(team_id).status == TeamStatus.INTERRUPTED
        assert harness.agents["glm"].calls == []
    finally:
        await harness.aclose()


async def test_shutdown_refuses_new_teams(tmp_path):
    settings = make_team_settings(tmp_path)
    harness = TeamHarness(settings, {"glm": FakeAgent("glm", reply="x")})
    try:
        await harness.runner.shutdown()
        assert await harness.runner.start_team("t00000000", 1) is False
    finally:
        await harness.aclose()


async def test_notifier_failure_does_not_rerun_steps(tmp_path):
    """外发通知失败只影响推送：步骤不重跑，结果仍可从存储取回。"""
    transport = FailingTransport()
    harness = TeamHarness(
        make_team_settings(tmp_path),
        {"glm": FakeAgent("glm", reply="A-成功")},
        transport=transport,
    )
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "a", "glm")
        harness.add_step(team_id, "b", "glm", after=("a",))
        harness.workflow_store.freeze_run(team_id, _snapshot(team_id, harness))
        assert await harness.runner.start_team(team_id, 1) is True
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        assert transport.failures > 0, "通知确实失败了"
        assert len(harness.agents["glm"].calls) == 2, "每个步骤只执行一次"
        assert harness.workflow_store.get_step_run(team_id, 1, "a").answer == "A-成功"
        assert harness.workflow_store.get_step_run(team_id, 1, "b").status == StepStatus.SUCCEEDED
    finally:
        await harness.aclose()


async def test_notifications_describe_frozen_mode_and_directory(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    counter = {"n": 0}

    def reply(agent, messages):
        counter["n"] += 1
        return json.dumps({"actions": [{"action": "list", "path": "."}]}) if counter["n"] == 1 \
            else json.dumps({"final": "完成"})

    harness = TeamHarness(
        workspace_settings(tmp_path, root=root),
        {"worker": FakeAgent("worker", reply=reply)},
    )
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "a", "worker", mode=MODE_READ, workspace=str(root))
        harness.workflow_store.freeze_run(team_id, _snapshot(team_id, harness))
        assert await harness.runner.start_team(team_id, 1) is True
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        texts = "\n".join(harness.transport.texts())
        assert f"权限 {MODE_READ}" in texts
        assert str(canonical(str(root))) in texts
        assert "完成" in texts
    finally:
        await harness.aclose()


async def test_legacy_plan_snapshot_runs_as_text(tmp_path):
    """重启后按旧快照解释：缺 mode/workspace 的步骤以 text 执行，不获得目录权限。"""
    harness = TeamHarness(make_team_settings(tmp_path), {"glm": FakeAgent("glm", reply="A")})
    try:
        team_id = harness.create_team()
        harness.add_step(team_id, "a", "glm")
        harness.workflow_store.freeze_run(team_id, _snapshot(team_id, harness))
        legacy = json.dumps({
            "team_id": team_id, "goal": "旧目标",
            "steps": [{
                "step_id": "a", "seq": 0, "agent": "glm", "instruction": "旧步骤",
                "depends_on": [], "model": "fake-glm", "effort": None,
            }],
        }, ensure_ascii=False)
        with harness.workflow_store._conn:
            harness.workflow_store._conn.execute(
                "UPDATE team_runs SET plan_json=? WHERE team_id=? AND round=1",
                (legacy, team_id),
            )
        assert await harness.runner.start_team(team_id, 1) is True
        assert await harness.wait_team(team_id, timeout=5) == TeamStatus.SUCCEEDED
        options = harness.agents["glm"].options_log[-1]
        assert options.mode == MODE_TEXT and options.directory is None
        assert harness.workflow_store.get_step_run(team_id, 1, "a").changed_files == 0
    finally:
        await harness.aclose()


def _snapshot(team_id: str, harness: TeamHarness) -> PlanSnapshot:
    steps = harness.workflow_store.list_steps(team_id)
    return PlanSnapshot(
        team_id=team_id, goal="目标",
        steps=tuple(
            FrozenStep(
                step_id=step.step_id, seq=step.seq, agent=step.agent,
                instruction=step.instruction, depends_on=step.depends_on,
                choice=FrozenChoice(
                    model=harness.backends.get(step.agent).descriptor.default_model
                ),
                mode=step.mode, backend=step.backend, workspace=step.workspace,
            )
            for step in steps
        ),
    )
