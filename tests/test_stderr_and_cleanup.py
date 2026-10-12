"""V02：stdout/stderr 并发排空、有界尾部保留、脱敏与有界收尾。

反例意图（必须保留，不能靠 skip/xfail 转绿）：
- 只读 stdout 时，子进程写满 stderr 管道缓冲区就会阻塞 → 任务应尽快失败而不是卡死；
- stderr 尾部必须是**整段输出**的末尾，而不是"先读 4000 字节再截 600 字符"；
- 取消/超时后台进程组、reader、pipe 都要在有限时间内回收，且不产生僵尸/孤儿。

这里用**真实子进程**（python 解释器 + 临时脚本）验证，不用假的 StreamReader。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

import pytest

from app.agents.codex import CodexAdapter, CodexCapabilities, BROKER_DISABLE_CANDIDATES
from app.agents.harness_process import StderrTailReader, TailBuffer
from app.models import ChatMessage, InvocationOptions


def _adapter(tmp_path: Path, *, binary: str | None = None) -> CodexAdapter:
    cache = tmp_path / "models.json"
    cache.write_text(json.dumps({"models": [{"slug": "test-model",
                                             "supported_reasoning_levels": [{"effort": "low"}]}]}))
    agent = CodexAdapter(True, binary=binary or sys.executable, workdir=str(tmp_path),
                         cache_path=str(cache))
    agent._login_present = True
    agent._capabilities = CodexCapabilities(
        binary=str(sys.executable), version="python-fixture",
        features=frozenset(BROKER_DISABLE_CANDIDATES), probed=True,
    )
    return agent


def _stub(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "stub.py"
    path.write_text(body, encoding="utf-8")
    return path


def _use_stub(agent: CodexAdapter, stub: Path, argv_sink: list | None = None) -> None:
    """把 CLI 换成临时脚本，但**保留生产 argv 的安全关键开关**。

    `_sandbox_ready()` 是 V03 的闸门：关键开关缺失时适配器会拒绝启动，所以这里的
    argv 必须与真实 argv 一样带齐 default_permissions/permissions/approval_policy/
    shell 环境/web_search；否则测到的是"拒绝启动"而不是进程清理。
    """
    from app.agents.codex import READ_ONLY_PROFILE, read_only_permission_config

    def build_argv(*, options, native, directory):
        argv = [
            sys.executable, str(stub),
            "-c", 'approval_policy="never"',
            "-c", f'default_permissions="{READ_ONLY_PROFILE}"',
            "-c", read_only_permission_config(),
            "-c", 'shell_environment_policy.inherit="none"',
            "-c", 'web_search="disabled"',
        ]
        if argv_sink is not None:
            argv_sink.append(argv)
        return argv, []

    agent.build_argv = build_argv  # type: ignore[method-assign]


# ---------- TailBuffer 的语义：容量有界，内容是最新字节 ----------


def test_tail_buffer_keeps_newest_bytes_within_limit() -> None:
    buffer = TailBuffer(16)
    buffer.feed(b"HEAD" * 10)
    assert buffer.size == 16
    assert buffer.value() == b"HEAD" * 4
    buffer.feed(b"TAIL")
    assert buffer.size == 16
    assert buffer.value().endswith(b"TAIL")
    assert buffer.value() == (b"HEAD" * 10 + b"TAIL")[-16:]
    # 单块远大于容量时只保留该块末尾
    small = TailBuffer(4)
    small.feed(b"0123456789")
    assert small.value() == b"6789"


# ---------- 1) 大量 stderr：必须立即失败，不得卡住 ----------


async def test_stderr_flood_returns_failure_without_hanging(tmp_path) -> None:
    agent = _adapter(tmp_path)
    stub = _stub(tmp_path, (
        "import os,sys\n"
        "sys.stdin.read()\n"
        "os.write(2, b'X' * (8 * 1024 * 1024))\n"
        "sys.exit(7)\n"
    ))
    _use_stub(agent, stub)
    started = time.monotonic()
    reply = await asyncio.wait_for(
        agent.chat_with_options([ChatMessage("user", "hello")], timeout=5,
                                options=InvocationOptions("test-model")),
        timeout=15,
    )
    elapsed = time.monotonic() - started
    assert reply.error == "codex_failed", reply
    assert elapsed < 12, f"stderr 排空/收尾耗时 {elapsed:.1f}s（疑似阻塞）"
    assert "CLI 退出码 7" in (reply.diagnostic or "")


# ---------- 2) stderr 尾部是整段输出的末尾，且总量有界 ----------


async def test_stderr_tail_is_real_tail_and_bounded(tmp_path) -> None:
    agent = _adapter(tmp_path)
    # 头部写 HEAD-ONLY，中间写 1 MiB 垃圾，末尾写 TAIL-ONLY
    stub = _stub(tmp_path, (
        "import os,sys\n"
        "sys.stdin.read()\n"
        "os.write(2, b'HEAD-ONLY\\n')\n"
        "os.write(2, b'M' * (1024 * 1024))\n"
        "os.write(2, b'\\nTAIL-ONLY\\n')\n"
        "sys.exit(3)\n"
    ))
    _use_stub(agent, stub)
    reply = await asyncio.wait_for(
        agent.chat_with_options([ChatMessage("user", "hi")], timeout=20,
                                options=InvocationOptions("test-model")),
        timeout=40,
    )
    assert reply.error == "codex_failed"
    diagnostic = reply.diagnostic or ""
    assert "TAIL-ONLY" in diagnostic, "stderr 尾部必须来自整段输出的末尾"
    assert "HEAD-ONLY" not in diagnostic, "不能只读前面一段再截尾"
    # 有界：尾部缓冲上限 8 KiB，加上诊断前缀也不应出现 1 MiB 垃圾
    assert len(diagnostic) < 40_000, len(diagnostic)


# ---------- 3) 脱敏 ----------


async def test_stderr_tail_is_redacted(tmp_path) -> None:
    agent = _adapter(tmp_path)
    stub = _stub(tmp_path, (
        "import os,sys\n"
        "sys.stdin.read()\n"
        "os.write(2, b'api_key=sk-shouldNotLeak1234\\n')\n"
        "os.write(2, b'token=abcdef0123456789\\n')\n"
        "sys.exit(9)\n"
    ))
    _use_stub(agent, stub)
    reply = await asyncio.wait_for(
        agent.chat_with_options([ChatMessage("user", "hi")], timeout=20,
                                options=InvocationOptions("test-model")),
        timeout=40,
    )
    diagnostic = reply.diagnostic or ""
    assert reply.error == "codex_failed"
    assert "sk-shouldNotLeak1234" not in diagnostic
    assert "abcdef0123456789" not in diagnostic
    assert "REDACTED" in diagnostic


# ---------- 4) 混合输出：stdout 事件正常解析，stderr 不干扰 ----------


async def test_mixed_stdout_stderr_keeps_events_and_tail(tmp_path) -> None:
    agent = _adapter(tmp_path)
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ANSWER_OK"}},
        {"type": "turn.completed"},
    ]
    payload = "".join(json.dumps(item) + "\n" for item in events)
    stub = _stub(tmp_path, (
        "import os,sys\n"
        "sys.stdin.read()\n"
        f"os.write(1, {payload!r}.encode())\n"
        "os.write(2, b'noise ' * 4096)\n"
        "sys.exit(0)\n"
    ))
    _use_stub(agent, stub)
    reply = await asyncio.wait_for(
        agent.chat_with_options([ChatMessage("user", "hi")], timeout=20,
                                options=InvocationOptions("test-model")),
        timeout=40,
    )
    assert reply.error is None, reply
    assert reply.content == "ANSWER_OK"


# ---------- 5) 取消：进程组被回收，且任务在有限时间内结束 ----------


async def test_cancellation_reclaims_process_group_and_reader(tmp_path) -> None:
    agent = _adapter(tmp_path)
    stub = _stub(tmp_path, (
        "import os,sys,time\n"
        "sys.stdin.read()\n"
        "os.write(2, b'noise\\n' * 1000)\n"
        "time.sleep(300)\n"
    ))
    _use_stub(agent, stub)
    task = asyncio.create_task(
        agent.chat_with_options([ChatMessage("user", "hi")], timeout=300,
                                options=InvocationOptions("test-model"))
    )
    await asyncio.sleep(0.5)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
    assert time.monotonic() - started < 15, "取消后的清理必须是有界的"


# ---------- 6) 超时：失败分类为 timeout，且清理有界 ----------


async def test_timeout_is_bounded_with_large_stderr(tmp_path) -> None:
    agent = _adapter(tmp_path)
    stub = _stub(tmp_path, (
        "import os,sys,time\n"
        "sys.stdin.read()\n"
        "os.write(2, b'Y' * (4 * 1024 * 1024))\n"
        "time.sleep(300)\n"
    ))
    _use_stub(agent, stub)
    started = time.monotonic()
    reply = await asyncio.wait_for(
        agent.chat_with_options([ChatMessage("user", "hi")], timeout=1,
                                options=InvocationOptions("test-model")),
        timeout=20,
    )
    assert reply.error == "timeout"
    assert time.monotonic() - started < 18


# ---------- 7) reader 超时不会挂死调用方 ----------


async def test_reader_finish_is_bounded() -> None:
    class StuckStream:
        async def read(self, _size: int) -> bytes:  # pragma: no cover - 故意永不返回
            await asyncio.sleep(3600)
            return b""

    reader = StderrTailReader(StuckStream(), limit=1024)
    reader.start()
    started = time.monotonic()
    tail = await reader.finish(timeout=0.2)
    assert tail == ""
    assert reader.error == "reader_timeout"
    assert time.monotonic() - started < 5


# ---------- R64：父进程先退出时仍回收后代（清理必须覆盖整组） ----------


async def test_cleanup_kills_descendants_after_parent_exits(tmp_path) -> None:
    """父进程启动同组子进程后先退出：后代仍必须被回收（有界）。"""
    import os
    import signal
    from app.agents.harness_process import kill_process, spawn_owned

    pidfile = tmp_path / "child.pid"
    child = "import time; time.sleep(60)"
    parent = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(p.pid))"
    )
    proc = await spawn_owned(sys.executable, "-c", parent,
                             stdout=asyncio.subprocess.PIPE,
                             stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    child_pid = int(pidfile.read_text())
    try:
        started = time.monotonic()
        await asyncio.wait_for(kill_process(proc), timeout=10)
        assert time.monotonic() - started < 10
        for _ in range(50):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("父进程已退出时未回收同组后代")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def test_cleanup_reclaims_descendant_holding_pipe(tmp_path) -> None:
    """后代持有 pipe 且父进程已退出时，清理仍需回收整组且保持有界。

    用同步 `subprocess.Popen(start_new_session=True)` 造出"父进程先退出、子进程仍在同一
    新会话/进程组"的真实场景（这与真实适配器的 spawn 形状一致），再用生产的
    `kill_process()` 回收；断言不依赖 asyncio 自己的子进程注册表。
    """
    import os
    import signal
    import subprocess as sp

    from app.agents.harness_process import kill_process, owns_process_group

    holder_log = tmp_path / "holder.log"
    holder = (f"import time,pathlib; pathlib.Path({str(holder_log)!r}).write_text('start'); "
              "time.sleep(120)")
    parent = ("import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c',"
              + repr(holder) + "]); pathlib.Path(" + repr(str(tmp_path / 'holder.pid'))
              + ").write_text(str(p.pid)); sys.exit(0)")
    parent_proc = sp.Popen([sys.executable, "-c", parent], start_new_session=True)
    assert parent_proc.wait(timeout=20) == 0
    holder_pid = int((tmp_path / "holder.pid").read_text())

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - 平台差异
            return True
        return True

    assert alive(holder_pid), "夹具失败：后代进程没有存活"
    assert os.getpgid(holder_pid) == parent_proc.pid, "夹具失败：后代不在新会话里"

    class ExitedParent:
        """父进程已退出的最小进程视图（真实调用里是 asyncio 的 Process）。"""

        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode = 0
            self._qq_owned_group = True

        async def wait(self) -> int:  # pragma: no cover - 已经退出，不会真的等
            return self.returncode

    view = ExitedParent(parent_proc.pid)
    assert owns_process_group(view) is True
    try:
        started = time.monotonic()
        await asyncio.wait_for(kill_process(view, wait=2.0), timeout=15)
        elapsed = time.monotonic() - started
        assert elapsed < 12, "后代持有 pipe 时清理必须有界"
        for _ in range(40):
            if not alive(holder_pid):
                break
            await asyncio.sleep(0.05)
        assert not alive(holder_pid), "父进程已退出时未回收同组后代"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(parent_proc.pid, signal.SIGKILL)


async def test_kill_process_does_not_touch_unowned_groups(tmp_path) -> None:
    """没有所有权标记、且 pid != pgid 的进程对象不得被 killpg（防误杀无关组）。"""
    import os
    import signal
    from app.agents import harness_process as hp

    killed: list[tuple[int, int]] = []

    class Foreign:
        pid = os.getpid()  # 属于 pytest 自己的进程组
        returncode = None

        async def wait(self):
            return -9

    monkeypatch_target = hp.killpg
    hp.killpg = lambda pgid, sig: killed.append((pgid, sig))
    try:
        await hp.kill_process(Foreign())
    finally:
        hp.killpg = monkeypatch_target
    assert not [item for item in killed if item[1] == signal.SIGKILL], killed


async def test_pipe_close_report_is_honest() -> None:
    """StreamReader 没有公开 close()：报告必须如实说明，而不是假装已关闭。"""
    from app.agents.harness_process import pipe_close_report, spawn_owned, kill_process

    proc = await spawn_owned(sys.executable, "-c", "import sys; sys.stdin.read()",
                             stdin=asyncio.subprocess.PIPE,
                             stdout=asyncio.subprocess.PIPE,
                             stderr=asyncio.subprocess.PIPE)
    report = pipe_close_report(proc)
    assert set(report) == {"stdin", "stdout", "stderr"}
    assert all(value in {"closable", "closable_via_transport", "no_public_close", "absent"}
               for value in report.values())
    await kill_process(proc)


# ---------- S64（二次复验）：所有权登记的生命周期必须闭合 ----------


async def test_cleanup_is_idempotent_and_consumes_registration(tmp_path) -> None:
    """重复清理必须幂等：第一次收尾后登记失效，不得再向（可能被复用的）旧组号发信号。"""
    import signal
    from types import SimpleNamespace
    from app.agents import harness_process as hp

    proc = SimpleNamespace(pid=987654321, returncode=0)
    hp.mark_owned_group(proc, pgid=proc.pid)
    state = {"alive": True}
    signals: list[tuple[int, int]] = []

    def missing(pid):
        raise ProcessLookupError()

    def fake_killpg(pgid, sig):
        if not state["alive"]:
            raise ProcessLookupError()
        if sig:
            signals.append((pgid, int(sig)))
            state["alive"] = False

    real_getpgid, real_killpg = hp.os.getpgid, hp.killpg
    hp.os.getpgid = missing
    hp.killpg = fake_killpg
    try:
        await hp.kill_process(proc, wait=0.01)
        assert [item for item in signals if item[1] == signal.SIGKILL] == [
            (proc.pid, signal.SIGKILL)]
        assert hp.ownership_state(proc) == "consumed"
        assert hp.owns_process_group(proc) is False
        # 组号被不相关进程复用：旧对象不得再次发 SIGKILL
        state["alive"] = True
        await hp.kill_process(proc, wait=0.01)
        await hp.kill_process(proc, wait=0.01)
        assert [item for item in signals if item[1] == signal.SIGKILL] == [
            (proc.pid, signal.SIGKILL)], signals
    finally:
        hp.os.getpgid, hp.killpg = real_getpgid, real_killpg


async def test_concurrent_cleanup_consumes_single_registration(tmp_path) -> None:
    """并发清理只应消耗一份旧登记（只发一次 SIGKILL）。"""
    import signal
    from types import SimpleNamespace
    from app.agents import harness_process as hp

    proc = SimpleNamespace(pid=987654322, returncode=0)
    hp.mark_owned_group(proc, pgid=proc.pid)
    state = {"alive": True}
    signals: list[tuple[int, int]] = []

    def missing(pid):
        raise ProcessLookupError()

    def fake_killpg(pgid, sig):
        if not state["alive"]:
            raise ProcessLookupError()
        if sig:
            signals.append((pgid, int(sig)))
            state["alive"] = False

    real_getpgid, real_killpg = hp.os.getpgid, hp.killpg
    hp.os.getpgid = missing
    hp.killpg = fake_killpg
    try:
        await asyncio.gather(hp.kill_process(proc, wait=0.01),
                             hp.kill_process(proc, wait=0.01),
                             hp.kill_process(proc, wait=0.01))
        assert len([item for item in signals if item[1] == signal.SIGKILL]) == 1, signals
    finally:
        hp.os.getpgid, hp.killpg = real_getpgid, real_killpg


async def test_release_owned_group_disables_further_signals() -> None:
    """显式释放（或 owned=False）后不得再 killpg。"""
    import signal
    from types import SimpleNamespace
    from app.agents import harness_process as hp

    proc = SimpleNamespace(pid=987654323, returncode=0)
    hp.mark_owned_group(proc, pgid=proc.pid)
    assert hp.ownership_state(proc) == "owned"
    hp.release_owned_group(proc)
    assert hp.ownership_state(proc) == "released"
    assert hp.owns_process_group(proc) is False
    assert hp.owned_group_id(proc) is None
    signals: list[tuple[int, int]] = []
    real_killpg = hp.killpg
    hp.killpg = lambda pgid, sig: signals.append((pgid, int(sig)))
    try:
        await hp.kill_process(proc, wait=0.01)
    finally:
        hp.killpg = real_killpg
    assert not [item for item in signals if item[1] == signal.SIGKILL]
    # owned=False 与显式释放等价
    hp.mark_owned_group(proc, pgid=proc.pid)
    hp.mark_owned_group(proc, owned=False)
    assert hp.ownership_state(proc) == "released"


async def test_reclaim_reports_consumed_ownership(tmp_path) -> None:
    """reclaim() 必须如实报告登记已被消耗，不能输出"仍拥有原组"。"""
    import os
    import signal
    from app.agents.harness_process import reclaim, spawn_owned

    proc = await spawn_owned(sys.executable, "-c", "import time; time.sleep(30)",
                             stdout=asyncio.subprocess.PIPE,
                             stderr=asyncio.subprocess.PIPE)
    try:
        report = await reclaim(proc, wait=2.0)
        assert report["ownership_before"] == "owned"
        assert report["ownership_after"] == "consumed"
        assert report["owned_group"] is False
        assert report["recorded_group"] == proc.pid
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
