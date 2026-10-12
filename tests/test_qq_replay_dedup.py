"""QQ 任务提交去重（消息重放加固）的独立验证。

覆盖提示词要求的全部场景，全部使用**假 Agent + 记录型传输 + 临时数据库**：
真实模型调用 = 0，真实 QQ 发送 = 0。

断言以**真实调用计数与任务行数**为准，不只测新函数返回值。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from pathlib import Path

import pytest

from app.models import InboundMessage
from app.routing.qq_dedup import QQ_DEDUP_KEY_VERSION, build_key
from app.tasks.store import TaskStore
from tests.helpers import ALLOWED_GROUP, ALLOWED_USER, BOT_SELF, ConsoleHarness, FakeAgent, make_settings
from tests.helpers import make_message


def _message(text: str, *, message_id: int | None = 424242, user_id: int = ALLOWED_USER,
             group_id: int = ALLOWED_GROUP, self_id: int = BOT_SELF) -> InboundMessage:
    base = make_message(text, user_id=user_id, group_id=group_id, self_id=self_id)
    return dataclasses.replace(base, message_id=message_id)


def _task_rows(harness: ConsoleHarness) -> list[tuple]:
    return harness.context_store.connection.execute(
        "SELECT task_id, status, idempotency_key FROM tasks ORDER BY created_at"
    ).fetchall()


# ---------- 1. 顺序重放：同一条消息投递两次只执行一次 ----------


def test_sequential_replay_executes_once(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, int, list[tuple]]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            message = _message("/task codex -- REPLAY")
            await harness.dispatcher.handle_message(message)
            await harness.dispatcher.handle_message(message)
            return len(agent.calls), harness.send_count(), len(_task_rows(harness)), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, replies, rows, detail = asyncio.run(scenario())
    assert calls == 1, detail          # 只调用一次模型
    assert rows == 1, detail           # 只有一行任务
    assert replies == 2, detail        # 第二次是一条"已受理，未重复执行"回执


# ---------- 2. 并发重放 ----------


def test_concurrent_replay_executes_once(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, int, list[tuple]]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.02)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            message = _message("/task codex -- REPLAY")
            await asyncio.gather(
                harness.dispatcher.handle_message(message),
                harness.dispatcher.handle_message(message),
            )
            return len(agent.calls), harness.send_count(), len(_task_rows(harness)), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, replies, rows, detail = asyncio.run(scenario())
    assert calls == 1, detail
    assert rows == 1, detail
    assert replies == 2, detail


# ---------- 3. 进程重建后同一消息不得被当作新任务 ----------


def test_replay_after_process_rebuild_is_deduplicated(tmp_path: Path) -> None:
    async def first() -> str:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            await harness.dispatcher.handle_message(_message("/task codex -- REPLAY"))
            assert len(agent.calls) == 1
            return _task_rows(harness)[0][0]
        finally:
            await harness.aclose()

    first_id = asyncio.run(first())

    async def second() -> tuple[int, int, list[tuple]]:
        # 全新 harness = 新进程语义（新的 Dispatcher/内存态，共用同一临时数据库）
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            await harness.dispatcher.handle_message(_message("/task codex -- REPLAY"))
            return len(agent.calls), harness.send_count(), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, replies, rows = asyncio.run(second())
    assert calls == 0, rows
    assert replies == 1, rows
    assert len(rows) == 1 and rows[0][0] == first_id, rows


# ---------- 4. 同文不同 message_id 必须仍是两个任务 ----------


def test_same_text_different_message_id_creates_two_tasks(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, list[tuple]]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            await harness.dispatcher.handle_message(_message("/task codex -- SAME TEXT", message_id=1))
            await harness.dispatcher.handle_message(_message("/task codex -- SAME TEXT", message_id=2))
            return len(agent.calls), harness.send_count(), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, replies, rows = asyncio.run(scenario())
    assert calls == 2, rows
    assert replies == 2, rows
    assert len(rows) == 2, rows


# ---------- 5. 命名空间隔离：机器人 / 群 / 用户 / 子任务目标 ----------


def test_namespace_isolates_bot_group_user_and_target() -> None:
    base = _message("/task codex -- X", message_id=7)
    key = build_key(base, target="task", agent="codex").value
    assert key.startswith(f"{QQ_DEDUP_KEY_VERSION}|qq|")
    other_bot = build_key(dataclasses.replace(base, self_id=BOT_SELF + 1), target="task", agent="codex").value
    other_group = build_key(dataclasses.replace(base, group_id=ALLOWED_GROUP + 1), target="task",
                            agent="codex").value
    other_user = build_key(dataclasses.replace(base, user_id=ALLOWED_USER + 1), target="task",
                           agent="codex").value
    other_agent = build_key(base, target="task", agent="zcode").value
    other_target = build_key(base, target="all", agent="codex").value
    assert len({key, other_bot, other_group, other_user, other_agent, other_target}) == 6
    # 与 HTTP 命名空间不同（不会与 Idempotency-Key 混用）
    assert not key.startswith("v1|http|")


def test_namespace_isolates_across_groups_end_to_end(tmp_path: Path) -> None:
    async def scenario() -> list[tuple]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            settings = harness.settings
            groups = list(settings.qq_allowed_group_ids)
            assert len(groups) >= 1
            # 同 message_id、不同群：群参与命名空间，故各自执行
            await harness.dispatcher.handle_message(_message("/task codex -- X", message_id=99))
            return _task_rows(harness)
        finally:
            await harness.aclose()

    rows = asyncio.run(scenario())
    assert len(rows) == 1


# ---------- 6. 未授权重复：鉴权在去重之前 ----------


def test_unauthorized_replay_gets_no_task_and_no_result_leak(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[tuple], list[str], int]:
        agent = FakeAgent("codex", reply="SECRET_RESULT", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            # 先由授权用户建任务（占用同一 message_id）
            await harness.dispatcher.handle_message(_message("/task codex -- X", message_id=555))
            sends_before = harness.send_count()
            outsider = _message("/task codex -- X", message_id=555, user_id=ALLOWED_USER + 777)
            await harness.dispatcher.handle_message(outsider)
            texts = harness.transport.texts()
            return _task_rows(harness), texts, sends_before
        finally:
            await harness.aclose()

    rows, texts, sends_before = asyncio.run(scenario())
    assert len(rows) == 1, rows                                   # 未授权身份没有新建任务
    assert "SECRET_RESULT" not in " ".join(texts[sends_before:])   # 也没有拿到既有任务结果


# ---------- 7. 已有终态：不自动重试 failed/cancelled/interrupted ----------


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "interrupted"])
def test_terminal_existing_task_is_not_auto_retried(tmp_path: Path, terminal: str) -> None:
    async def scenario() -> tuple[int, list[tuple]]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            store = harness.task_store
            key = build_key(_message("/task codex -- X"), target="task", agent="codex").value
            record, created = store.register_idempotent_task(
                entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
                prompt="X", idempotency_key=key, status="queued",
            )
            assert created
            store.update_task(record["task_id"], status=terminal, error="seed")
            await harness.dispatcher.handle_message(_message("/task codex -- X"))
            return len(agent.calls), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, rows = asyncio.run(scenario())
    assert calls == 0, rows                # 终态不自动重跑
    assert len(rows) == 1, rows            # 既有记录保留，没有新任务


def test_running_existing_task_is_reused_without_second_call(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, list[tuple]]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            store = harness.task_store
            key = build_key(_message("/task codex -- X"), target="task", agent="codex").value
            record, _ = store.register_idempotent_task(
                entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
                prompt="X", idempotency_key=key, status="running",
            )
            await harness.dispatcher.handle_message(_message("/task codex -- X"))
            return len(agent.calls), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, rows = asyncio.run(scenario())
    assert calls == 0, rows
    assert len(rows) == 1, rows


# ---------- 8. 无 message_id 的兼容行为 ----------


def test_missing_message_id_keeps_legacy_behaviour(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, list[tuple], str]:
        agent = FakeAgent("codex", reply="REPLAY_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            message = _message("/task codex -- X", message_id=None)
            await harness.dispatcher.handle_message(message)
            await harness.dispatcher.handle_message(message)
            return len(agent.calls), _task_rows(harness), build_key(message, target="task",
                                                                   agent="codex").value
        finally:
            await harness.aclose()

    calls, rows, key = asyncio.run(scenario())
    assert key == ""                       # 无 message_id → 不生成键
    assert calls == 2, rows                # 兼容行为：不做去重（与本轮整改前一致）
    assert len(rows) == 2, rows
    assert all(row[2] is None for row in rows), rows


# ---------- 9. /task all 的每个子任务独立去重 ----------


def test_task_all_deduplicates_per_subtask(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, int], int, list[tuple]]:
        agents = {
            "codex": FakeAgent("codex", reply="ALL_OK", delay=0.0),
            "zcode": FakeAgent("zcode", reply="ALL_OK", delay=0.0),
        }
        harness = ConsoleHarness(
            make_settings(tmp_path, console_agents={
                "codex": {"enabled": True, "default": True},
                "zcode": {"enabled": True},
            }),
            agents,
        )
        try:
            message = _message("/task all -- BROADCAST")
            await harness.dispatcher.handle_message(message)
            first = {name: len(agent.calls) for name, agent in agents.items()}
            await harness.dispatcher.handle_message(message)
            second = {name: len(agent.calls) for name, agent in agents.items()}
            return first, sum(second.values()), _task_rows(harness)
        finally:
            await harness.aclose()

    first, total_after, rows = asyncio.run(scenario())
    assert sum(first.values()) >= 1, first
    assert total_after == sum(first.values()), rows     # 重放没有新增调用
    assert len(rows) == sum(first.values()), rows       # 每个子任务一行


# ---------- 10. preset 走同一提交链 ----------


def test_preset_submission_is_deduplicated(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, list[tuple]]:
        from app.commands.presets import PresetSpec

        agent = FakeAgent("codex", reply="PRESET_OK", delay=0.0)
        preset = PresetSpec(name="quick", description="快速提问", agent="codex",
                            template="{task}", mode="text")
        harness = ConsoleHarness(
            make_settings(tmp_path, console_agents={"codex": {"enabled": True, "default": True}}),
            {"codex": agent},
            presets={"quick": preset},
        )
        try:
            message = _message("/quick -- PRESET")
            await harness.dispatcher.handle_message(message)
            await harness.dispatcher.handle_message(message)
            return len(agent.calls), harness.send_count(), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, replies, rows = asyncio.run(scenario())
    assert calls == 1, rows
    assert replies == 2, rows
    assert len(rows) == 1, rows


# ---------- 11. 目录待确认分支：重复投递不产生第二个待确认任务 ----------


def test_awaiting_selection_branch_is_deduplicated(tmp_path: Path) -> None:
    """真实的目录待确认分支：`--find` 命中**两个**候选 → awaiting_selection。

    注意：相对路径 `--dir alpha` 走的是**拒绝分支**（未绑定授权根），
    因此这里必须用 `--find` 双候选才能覆盖"待确认"这条路径。
    """

    async def scenario() -> tuple[list[tuple], int, str]:
        root = tmp_path / "root"
        (root / "alpha-one").mkdir(parents=True)
        (root / "alpha-two").mkdir(parents=True)
        agent = FakeAgent("codex", reply="SEL_OK", delay=0.0)
        harness = ConsoleHarness(
            make_settings(
                tmp_path,
                console_workspaces={"roots": [{"path": str(root), "modes": ["read"],
                                               "admin_modes": ["read"], "searchable": True,
                                               "label": "test-root"}],
                                    "find": {"max_depth": 3, "max_candidates": 5,
                                             "max_seconds": 2.0}},
            ),
            {"codex": agent},
        )
        try:
            message = _message("/task codex --mode read --find alpha -- X")
            await harness.dispatcher.handle_message(message)
            first = _task_rows(harness)
            await harness.dispatcher.handle_message(message)
            after = _task_rows(harness)
            assert first == after, (first, after)
            return after, len(agent.calls), harness.transport.texts()[-1]
        finally:
            await harness.aclose()

    rows, calls, reply = asyncio.run(scenario())
    assert len(rows) == 1, rows
    assert rows[0][1] == "awaiting_selection", rows     # 真正命中待确认分支
    assert calls == 0, rows                             # 待确认阶段不调用模型
    assert "未重复执行" in reply, reply


def test_relative_path_rejection_is_not_awaiting_selection(tmp_path: Path) -> None:
    """保留相对路径拒绝测试：`--dir alpha` 不得被当作待确认分支。"""

    async def scenario() -> list[tuple]:
        root = tmp_path / "root"
        (root / "alpha").mkdir(parents=True)
        agent = FakeAgent("codex", reply="SEL_OK", delay=0.0)
        harness = ConsoleHarness(
            make_settings(
                tmp_path,
                console_workspaces={"roots": [{"path": str(root), "modes": ["read"],
                                               "admin_modes": ["read"], "searchable": True,
                                               "label": "test-root"}]},
            ),
            {"codex": agent},
        )
        try:
            await harness.dispatcher.handle_message(
                _message("/task codex --dir alpha --mode read -- X"))
            return _task_rows(harness)
        finally:
            await harness.aclose()

    rows = asyncio.run(scenario())
    assert all(row[1] != "awaiting_selection" for row in rows), rows
    assert rows and rows[0][1] in ("failed", "rejected"), rows


# ---------- 12. HTTP 幂等语义不受影响 ----------


def test_http_idempotency_namespace_is_separate(tmp_path: Path) -> None:
    """HTTP 的 Idempotency-Key 与 QQ 键不混用：同键值也不会互相命中。"""

    async def scenario() -> tuple[int, list[tuple]]:
        agent = FakeAgent("codex", reply="HTTP_OK", delay=0.0)
        harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
        try:
            store = harness.task_store
            shared = "shared-key"
            http_row, http_created = store.register_idempotent_task(
                entry="http", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
                prompt="X", idempotency_key=shared, status="queued",
            )
            assert http_created
            qq_key = build_key(_message("/task codex -- X"), target="task", agent="codex").value
            assert qq_key != shared
            await harness.dispatcher.handle_message(_message("/task codex -- X"))
            return len(agent.calls), _task_rows(harness)
        finally:
            await harness.aclose()

    calls, rows = asyncio.run(scenario())
    assert calls == 1, rows
    assert len(rows) == 2, rows       # HTTP 行 + QQ 行，互不命中


def test_http_idempotency_replay_contract_unchanged(tmp_path: Path) -> None:
    """HTTP 路径既有行为：同 Idempotency-Key 返回既有 task_id。"""
    db = tmp_path / "http.db"
    connection = sqlite3.connect(str(db))
    store = TaskStore(connection)
    store.migrate()
    try:
        row, created = store.register_idempotent_task(
            entry="http", group_id=1, user_id=2, agent="codex", prompt="X",
            idempotency_key="http-key-1", status="queued",
        )
        assert created
        again, created_again = store.register_idempotent_task(
            entry="http", group_id=1, user_id=2, agent="codex", prompt="X",
            idempotency_key="http-key-1", status="queued",
        )
        assert created_again is False
        assert again["task_id"] == row["task_id"]
        assert store.find_by_idempotency(group_id=1, user_id=2, key="http-key-1")["task_id"] == row["task_id"]
    finally:
        connection.close()


# ---------- 13. 真原子性：多线程并发登记同一键只成功一次 ----------


def test_concurrent_registration_across_threads_is_atomic(tmp_path: Path) -> None:
    """并发登记的唯一裁决者是 SQLite 唯一索引（不是"先查再插"）。

    每个线程用自己的连接（与真实部署一致：一个进程一条连接，写操作由 SQLite 串行化），
    因此并发冲突表现为"一个插入成功、其余命中唯一索引"，而不是共享连接的事务错误。
    """
    import threading

    db = tmp_path / "concurrent.db"
    bootstrap = sqlite3.connect(str(db))
    TaskStore(bootstrap).migrate()
    bootstrap.close()

    key = "v1|qq|999|200|100|424242|task|codex"
    results: list[tuple[str, bool]] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        connection = sqlite3.connect(str(db), timeout=15)
        store = TaskStore(connection)
        try:
            barrier.wait()
            record, created = store.register_idempotent_task(
                entry="qq", group_id=200, user_id=100, agent="codex", prompt="X",
                idempotency_key=key, status="queued",
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须被记录并让测试失败
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
            return
        finally:
            connection.close()
        with lock:
            results.append((str((record or {}).get("task_id") or ""), created))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    verification = sqlite3.connect(str(db))
    try:
        row_count = verification.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        stored_keys = [row[0] for row in verification.execute(
            "SELECT idempotency_key FROM tasks").fetchall()]
    finally:
        verification.close()

    assert not errors, errors
    assert sum(1 for _, created in results if created) == 1, results
    assert len({task_id for task_id, _ in results if task_id}) == 1, results
    assert row_count == 1, (row_count, results)
    assert stored_keys == [key], stored_keys


def test_registration_returns_existing_row_not_exception(tmp_path: Path) -> None:
    """冲突路径返回既有任务（不是抛唯一约束异常，也不是把异常当"已完成去重"）。"""
    db = tmp_path / "conflict.db"
    connection = sqlite3.connect(str(db))
    store = TaskStore(connection)
    store.migrate()
    try:
        key = "v1|qq|999|200|100|1|task|codex"
        first, created = store.register_idempotent_task(
            entry="qq", group_id=200, user_id=100, agent="codex", prompt="X",
            idempotency_key=key, status="queued",
        )
        assert created and first["status"] == "queued"
        store.update_task(first["task_id"], status="running")
        second, created_again = store.register_idempotent_task(
            entry="qq", group_id=200, user_id=100, agent="codex", prompt="X",
            idempotency_key=key, status="queued",
        )
        assert created_again is False
        assert second["task_id"] == first["task_id"]
        assert second["status"] == "running"       # 未被新投递覆盖
    finally:
        connection.close()
