"""Harness 最终收尾：在既有 Codex 反例之外补的边界反例（F1/F2/F4）。

这些用例先以真实失败暴露缺口，再做最小修复；断言只看**任务行状态/数量**与
**真实调用计数**，不依赖实现细节。全部使用假 Agent + 记录型传输 + 临时数据库：
真实模型调用 0、真实 QQ 发送 0。
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sqlite3

import httpx
import pytest

from app.routing.qq_dedup import HTTP_KEY_SCHEME, build_key
from app.tasks.store import IdempotencyKeyConflict, TaskStore
from app.workspaces.models import WorkspaceError
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    make_message,
    make_settings,
    workspace_settings,
)
from test_api_v1 import _drain, auth, build_api_app

_BODY = {"agent": "codex", "prompt": "X", "group_id": ALLOWED_GROUP}


def _rows(harness: ConsoleHarness) -> list[tuple]:
    return harness.context_store.connection.execute(
        "SELECT task_id, entry, status, error, idempotency_key "
        "FROM tasks ORDER BY created_at"
    ).fetchall()


def _message(text: str, *, message_id: int | None = 424242):
    return dataclasses.replace(make_message(text), message_id=message_id)


def _selection_code(texts: list[str]) -> str:
    for text in reversed(texts):
        match = re.search(r"/workspace select (\S+)", text)
        if match:
            return match.group(1)
    raise AssertionError(f"回执里没有确认码：{texts}")


# ---------- F2：HTTP 新键与旧原始键不得互相冒名命中 ----------


async def test_http_namespaced_row_not_impersonated_by_its_literal_key(tmp_path) -> None:
    """另一客户端的原始 key 恰好等于新方案的**存储键**时不得冒名命中。"""
    agent = FakeAgent("codex", reply="HTTP_OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "alpha"},
                json={**_BODY, "prompt": "FIRST"},
            )
            assert first.status_code == 202, first.text
            await _drain(app)
            # 另一个请求的原始 key 字面等于第一条记录的存储键 `v1|http|alpha`
            second = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "v1|http|alpha"},
                json={**_BODY, "prompt": "SECOND"},
            )
            assert second.status_code == 202, second.text
            assert second.json().get("idempotent") is not True, second.json()
            assert second.json()["task_id"] != first.json()["task_id"], second.json()
            await _drain(app)
        rows = _rows(harness)
        assert len(rows) == 2, rows
        assert len(agent.calls) == 2, rows
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_legacy_row_not_impersonated_by_new_namespaced_key(tmp_path) -> None:
    """旧原始键记录（无方案标记）不得被"新键恰好等于该原始键"的请求冒名命中。"""
    agent = FakeAgent("codex", reply="HTTP_OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    legacy, created = harness.task_store.register_idempotent_task(
        entry="http", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
        prompt="OLD", mode="text", status="succeeded", idempotency_key="v1|http|beta",
    )
    assert created
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "beta"},
                json={**_BODY, "prompt": "NEW"},
            )
            assert response.status_code == 202, response.text
            assert response.json().get("idempotent") is not True, response.json()
            assert response.json()["task_id"] != legacy["task_id"], response.json()
            await _drain(app)
        rows = _rows(harness)
        assert len(rows) == 2, rows
        assert len(agent.calls) == 1, rows
    finally:
        await _drain(app)
        await harness.aclose()


def test_store_namespaced_row_survives_literal_legacy_collision(tmp_path) -> None:
    """新方案记录与"字面相同的旧原始键"必须能共存且各自可查（不 phantom duplicate）。"""
    connection = sqlite3.connect(str(tmp_path / "collide.db"))
    store = TaskStore(connection)
    store.migrate()
    try:
        legacy, created = store.register_idempotent_task(
            entry="http", group_id=1, user_id=2, agent="codex", prompt="OLD",
            status="succeeded", idempotency_key="v1|http|gamma",
        )
        assert created
        fresh, created_again = store.register_idempotent_task(
            entry="http", group_id=1, user_id=2, agent="codex", prompt="NEW",
            status="queued", idempotency_key="v1|http|gamma", key_scheme=HTTP_KEY_SCHEME,
        )
        assert created_again is True, fresh
        assert fresh["task_id"] != legacy["task_id"]
        # 旧记录仍只按旧方案命中，新记录仍只按新方案命中
        found_legacy = store.find_by_idempotency(
            group_id=1, user_id=2, key="v1|http|gamma", entry="http", key_scheme=None
        )
        assert found_legacy is not None and found_legacy["task_id"] == legacy["task_id"]
        found_fresh = store.find_by_idempotency(
            group_id=1, user_id=2, key="v1|http|gamma#2", entry="http",
            key_scheme=HTTP_KEY_SCHEME,
        )
        assert found_fresh is not None and found_fresh["task_id"] == fresh["task_id"]
        # 同一新方案键再登记：命中同一个新行，不再新建
        again, created_third = store.register_idempotent_task(
            entry="http", group_id=1, user_id=2, agent="codex", prompt="NEW",
            status="queued", idempotency_key="v1|http|gamma", key_scheme=HTTP_KEY_SCHEME,
        )
        assert created_third is False
        assert again["task_id"] == fresh["task_id"]
        count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 2, count
    finally:
        connection.close()


async def test_legacy_http_row_cannot_swallow_literal_qq_key(tmp_path) -> None:
    """旧 HTTP 原始键字面等于 QQ 整串键时，QQ 提交不得被该跨入口记录吞掉。"""
    agent = FakeAgent("codex", reply="QQ_OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    try:
        message = _message("/task codex -- QQ_BODY")
        qq_key = build_key(message, target="task", agent="codex").value
        legacy, created = harness.task_store.register_idempotent_task(
            entry="http", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
            prompt="HTTP_OLD", mode="text", status="succeeded", idempotency_key=qq_key,
        )
        assert created
        await harness.dispatcher.handle_message(message)
        rows = _rows(harness)
        assert len(agent.calls) == 1, rows
        assert len(rows) == 2, rows
        qq_rows = [row for row in rows if row[1] == "qq"]
        assert len(qq_rows) == 1, rows
        assert qq_rows[0][0] != legacy["task_id"], rows
        assert qq_rows[0][2] in ("succeeded", "no_change"), rows
    finally:
        await harness.aclose()


async def test_http_concurrent_same_key_creates_one_task(tmp_path) -> None:
    """并发同键：只登记一行、只执行一次、两个响应指向同一 task_id。"""
    agent = FakeAgent("codex", reply="HTTP_OK", delay=0.02)
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {**auth(), "Idempotency-Key": "concurrent-key"}
            first, second = await asyncio.gather(
                client.post("/api/v1/tasks", headers=headers, json={**_BODY, "prompt": "A"}),
                client.post("/api/v1/tasks", headers=headers, json={**_BODY, "prompt": "B"}),
            )
            assert first.status_code == second.status_code == 202, (first.text, second.text)
            assert first.json()["task_id"] == second.json()["task_id"], (first.json(), second.json())
            await _drain(app)
        rows = _rows(harness)
        assert len(rows) == 1, rows
        assert len(agent.calls) == 1, rows
    finally:
        await _drain(app)
        await harness.aclose()


# ---------- F4：预登记之后的拒绝/异常出口不得覆盖取消终态 ----------


async def test_selector_error_does_not_overwrite_cancelled_terminal(tmp_path) -> None:
    """目录解析抛 WorkspaceError 时，若预登记行已被并发取消，必须保留取消终态。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})

    def resolve_selector(**kwargs):
        task_id = str(kwargs.get("task_id") or "")
        assert task_id, kwargs
        # 模拟并发取消（如撤权 cancel_all_for 恰好在此刻终结预登记行）
        harness.task_store.update_status_if_active(task_id, "cancelled", error="cancelled")
        raise WorkspaceError("没有可搜索的授权根")

    harness.workspaces.resolve_selector = resolve_selector
    try:
        await harness.dispatcher.handle_message(
            _message("/task codex --mode read --find alpha -- X", message_id=515151)
        )
        rows = _rows(harness)
        assert len(rows) == 1, rows
        assert rows[0][2] == "cancelled", rows
        assert rows[0][4], rows          # 幂等键保留（同消息重放不会重跑）
        assert len(agent.calls) == 0, rows
    finally:
        await harness.aclose()


async def test_broadcast_spec_rejection_does_not_overwrite_cancelled_terminal(tmp_path) -> None:
    """`/task all` 规格失败出口同样不得把并发取消终态改写成 failed。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})

    def build_spec(*, request, workspace, mode):
        if request.task_id:
            harness.task_store.update_status_if_active(
                str(request.task_id), "cancelled", error="cancelled"
            )
        return None, "invalid option: not-a-valid-model"

    harness.tasks.build_spec = build_spec
    try:
        await harness.dispatcher.handle_message(_message("/task all -- X", message_id=525252))
        rows = _rows(harness)
        assert rows, rows
        assert all(row[2] == "cancelled" for row in rows), rows
        assert all(row[4] for row in rows), rows
        assert len(agent.calls) == 0, rows
    finally:
        await harness.aclose()


async def test_http_workspace_rejection_is_terminal_and_replay_is_idempotent(tmp_path) -> None:
    """HTTP 目录拒绝：预登记行收敛为终态并保留键；同键重放返回既有任务、不重跑。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    app = build_api_app(harness)
    payload = {"agent": "codex", "prompt": "X", "mode": "read", "find": "alpha",
               "group_id": ALLOWED_GROUP}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {**auth(), "Idempotency-Key": "reject-key"}
            first = await client.post("/api/v1/tasks", headers=headers, json=payload)
            assert first.status_code == 400, first.text
            rows = _rows(harness)
            assert len(rows) == 1 and rows[0][2] == "failed", rows
            assert rows[0][4], rows
            replay = await client.post("/api/v1/tasks", headers=headers, json=payload)
            assert replay.status_code == 202, replay.text
            assert replay.json().get("idempotent") is True, replay.json()
            assert replay.json()["task_id"] == rows[0][0], replay.json()
            await _drain(app)
        assert len(agent.calls) == 0, agent.calls
        assert len(_rows(harness)) == 1, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


async def test_unexpected_run_error_does_not_leave_queued_reservation(tmp_path) -> None:
    """预登记之后的非预期异常：收敛为 failed 终态（保留键、脱敏诊断），不永久 queued。"""
    root = tmp_path / "root"
    root.mkdir()
    agent = FakeAgent("codex")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"codex": agent})

    def boom(**_kwargs):
        raise RuntimeError("broker-init-failed")

    harness.tasks._make_broker = boom  # noqa: SLF001 - 故意注入非预期异常
    try:
        await harness.dispatcher.handle_message(
            _message(f'/task codex --mode read --dir "{root}" -- X', message_id=535353)
        )
        rows = _rows(harness)
        assert len(rows) == 1, rows
        assert rows[0][2] == "failed", rows
        assert rows[0][3] == "internal_error", rows
        assert rows[0][4], rows
        assert len(agent.calls) == 0, rows
    finally:
        await harness.aclose()


async def test_resume_after_cancel_does_not_execute(tmp_path) -> None:
    """待选择任务被取消后，目录确认不得让它复活执行。"""
    root = tmp_path / "root"
    (root / "alpha-one").mkdir(parents=True)
    (root / "alpha-two").mkdir()
    agent = FakeAgent("codex", reply="SEL_OK")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"codex": agent})
    try:
        message = _message("/task codex --mode read --find alpha -- X", message_id=545454)
        await harness.dispatcher.handle_message(message)
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][2] == "awaiting_selection", rows
        task_id = rows[0][0]
        code = _selection_code(harness.transport.texts())
        await harness.dispatcher.handle_message(make_message(f"/task cancel {task_id}"))
        assert _rows(harness)[0][2] == "cancelled", _rows(harness)
        await harness.dispatcher.handle_message(make_message(f"/workspace select {code} 1"))
        assert len(agent.calls) == 0, agent.calls
        assert _rows(harness)[0][2] == "cancelled", _rows(harness)
    finally:
        await harness.aclose()


async def test_resume_race_with_cancel_does_not_execute(tmp_path) -> None:
    """取消恰好发生在"读到待选择 → 置回 queued"之间时，不得覆盖取消终态去执行。"""
    root = tmp_path / "root"
    (root / "alpha-one").mkdir(parents=True)
    (root / "alpha-two").mkdir()
    agent = FakeAgent("codex", reply="SEL_OK")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"codex": agent})
    original = harness.tasks.own_task

    def own_task(task_id, *, group_id, user_id):
        record = original(task_id, group_id=group_id, user_id=user_id)
        if record is not None and record["status"] == "awaiting_selection":
            # 模拟并发取消：resume 已读到旧状态，取消随后落库
            harness.task_store.update_status_if_active(task_id, "cancelled", error="cancelled")
        return record

    harness.tasks.own_task = own_task
    try:
        message = _message("/task codex --mode read --find alpha -- X", message_id=555555)
        await harness.dispatcher.handle_message(message)
        code = _selection_code(harness.transport.texts())
        await harness.dispatcher.handle_message(make_message(f"/workspace select {code} 1"))
        assert len(agent.calls) == 0, agent.calls
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][2] == "cancelled", rows
    finally:
        await harness.aclose()


# ---------- F1：目录选择后续跑一次、重放不再执行 ----------


async def test_qq_selection_resume_runs_once_and_replay_does_not_rerun(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "alpha-one").mkdir(parents=True)
    (root / "alpha-two").mkdir()
    agent = FakeAgent("codex", reply="SEL_OK")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"codex": agent})
    try:
        message = _message("/task codex --mode read --find alpha -- X", message_id=565656)
        await harness.dispatcher.handle_message(message)
        first = _rows(harness)
        assert len(first) == 1 and first[0][2] == "awaiting_selection", first
        assert len(agent.calls) == 0, first
        code = _selection_code(harness.transport.texts())
        await harness.dispatcher.handle_message(make_message(f"/workspace select {code} 1"))
        calls_after_resume = len(agent.calls)
        assert calls_after_resume >= 1, first
        # 同一确认码二次使用：被拒绝，不产生第二次执行
        await harness.dispatcher.handle_message(make_message(f"/workspace select {code} 1"))
        assert len(agent.calls) == calls_after_resume, agent.calls
        # 原消息重放：仍是同一行、不再执行
        await harness.dispatcher.handle_message(message)
        assert len(agent.calls) == calls_after_resume, agent.calls
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][0] == first[0][0], rows
        assert "未重复执行" in harness.transport.texts()[-1], harness.transport.texts()[-1]
    finally:
        await harness.aclose()


async def test_registration_conflict_without_visible_row_does_not_execute(tmp_path) -> None:
    """原子登记冲突但读不到既有行时：宁可少执行（拒绝），绝不重复执行。"""
    agent = FakeAgent("codex", reply="SHOULD_NOT_RUN")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    harness.task_store.register_idempotent_task = (  # type: ignore[method-assign]
        lambda **_kwargs: (None, False)
    )
    try:
        await harness.dispatcher.handle_message(_message("/task codex -- X", message_id=575757))
        assert len(agent.calls) == 0, agent.calls
        count = harness.context_store.connection.execute(
            "SELECT COUNT(*) FROM tasks"
        ).fetchone()[0]
        assert count == 0, count
        assert harness.send_count() == 1, harness.transport.texts()
    finally:
        await harness.aclose()


# ---------- F2（第二轮）：存储变体槽位必须校验真实逻辑键归属 ----------


async def test_http_hash_suffixed_raw_keys_are_distinct_and_replayable(tmp_path) -> None:
    """raw=A#2 / A / A#3 是三个合法不同的提交：不得因 `#n` 变体槽位互相冒名。"""
    agent = FakeAgent("codex", reply="OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    app = build_api_app(harness)
    raw_keys = ("A#2", "A", "A#3")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ids = []
            for key in raw_keys:
                response = await client.post(
                    "/api/v1/tasks", headers={**auth(), "Idempotency-Key": key},
                    json={**_BODY, "prompt": key},
                )
                assert response.status_code == 202, response.text
                ids.append(response.json()["task_id"])
                await _drain(app)
            assert len(set(ids)) == 3, ids
            assert len(agent.calls) == 3, agent.calls
            for key, task_id in zip(raw_keys, ids):
                replay = await client.post(
                    "/api/v1/tasks", headers={**auth(), "Idempotency-Key": key},
                    json={**_BODY, "prompt": key},
                )
                assert replay.status_code == 202, replay.text
                assert replay.json()["task_id"] == task_id, (key, replay.json())
            await _drain(app)
        assert len(agent.calls) == 3, agent.calls
        assert len(_rows(harness)) == 3, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_shadowed_variant_slot_is_not_claimed_by_another_logical_key(
    tmp_path,
) -> None:
    """raw=A 因旧行占基键而落到 `v1|http|A#2` 变体槽后，raw=A#2 必须各自成任务。

    该槽位字面等于 raw=A#2 的基键：只有登记时记录的真实逻辑键能区分归属，
    因此查询与原子登记都不能只看 entry/key_scheme。
    """
    agent = FakeAgent("codex", reply="OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    legacy, created = harness.task_store.register_idempotent_task(
        entry="http", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
        prompt="OLD", mode="text", status="succeeded",
        idempotency_key="v1|http|A",  # 旧原始键恰好等于 raw=A 的新基键
    )
    assert created
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "A"},
                json={**_BODY, "prompt": "RAW_A"},
            )
            assert first.status_code == 202, first.text
            assert first.json().get("idempotent") is not True, first.json()
            assert first.json()["task_id"] != legacy["task_id"], first.json()
            second = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "A#2"},
                json={**_BODY, "prompt": "RAW_A#2"},
            )
            assert second.status_code == 202, second.text
            assert second.json().get("idempotent") is not True, second.json()
            assert second.json()["task_id"] not in (first.json()["task_id"], legacy["task_id"]), \
                second.json()
            await _drain(app)
            for key, task_id in (("A", first.json()["task_id"]),
                                 ("A#2", second.json()["task_id"])):
                replay = await client.post(
                    "/api/v1/tasks", headers={**auth(), "Idempotency-Key": key},
                    json={**_BODY, "prompt": key},
                )
                assert replay.status_code == 202, replay.text
                assert replay.json()["task_id"] == task_id, (key, replay.json())
                assert replay.json().get("idempotent") is True, (key, replay.json())
            await _drain(app)
        assert len(agent.calls) == 2, agent.calls
        assert len(_rows(harness)) == 3, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_legacy_occupation_variant_replay_is_same_task(tmp_path) -> None:
    """旧行占用基键后：变体行记录真实逻辑键，同一 raw key 重放命中同一任务、不重跑。"""
    agent = FakeAgent("codex", reply="OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    legacy, created = harness.task_store.register_idempotent_task(
        entry="http", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
        prompt="OLD", mode="text", status="succeeded",
        idempotency_key="v1|http|gamma",
    )
    assert created
    app = build_api_app(harness)
    headers = {**auth(), "Idempotency-Key": "gamma"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/tasks", headers=headers, json=_BODY)
            assert first.status_code == 202, first.text
            assert first.json().get("idempotent") is not True, first.json()
            assert first.json()["task_id"] != legacy["task_id"], first.json()
            await _drain(app)
            replay = await client.post("/api/v1/tasks", headers=headers, json=_BODY)
            assert replay.status_code == 202, replay.text
            assert replay.json().get("idempotent") is True, replay.json()
            assert replay.json()["task_id"] == first.json()["task_id"], replay.json()
            await _drain(app)
        assert len(agent.calls) == 1, agent.calls
        assert len(_rows(harness)) == 2, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


def test_store_variant_registration_records_logical_key_across_threads(tmp_path) -> None:
    """旧行占基键后：多线程并发登记同一逻辑键只新建一行，且记录真实逻辑键供重放命中。"""
    import threading

    db = tmp_path / "variant-concurrent.db"
    bootstrap = sqlite3.connect(str(db))
    TaskStore(bootstrap).migrate()
    bootstrap.close()
    seed = sqlite3.connect(str(db))
    try:
        legacy, created = TaskStore(seed).register_idempotent_task(
            entry="http", group_id=1, user_id=2, agent="codex", prompt="OLD",
            status="succeeded", idempotency_key="v1|http|A",
        )
        assert created
    finally:
        seed.close()

    results: list[tuple[str, bool, str, str]] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker() -> None:
        connection = sqlite3.connect(str(db), timeout=15)
        store = TaskStore(connection)
        try:
            barrier.wait()
            record, is_new = store.register_idempotent_task(
                entry="http", group_id=1, user_id=2, agent="codex", prompt="NEW",
                status="queued", idempotency_key="v1|http|A", key_scheme=HTTP_KEY_SCHEME,
                logical_key="v1|http|A",
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须被记录并让测试失败
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
            return
        finally:
            connection.close()
        with lock:
            results.append((
                str((record or {}).get("task_id") or ""), is_new,
                str((record or {}).get("idempotency_key") or ""),
                str((record or {}).get("logical_key") or ""),
            ))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    verification = sqlite3.connect(str(db))
    try:
        row_count = verification.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        verification.close()

    assert not errors, errors
    assert sum(1 for _, is_new, _, _ in results if is_new) == 1, results
    assert len({task_id for task_id, _, _, _ in results if task_id}) == 1, results
    assert {stored for _, _, stored, _ in results} == {"v1|http|A#2"}, results
    assert {logical for _, _, _, logical in results} == {"v1|http|A"}, results
    assert row_count == 2, (row_count, results)
    assert legacy["task_id"] != results[0][0], results


def test_store_variant_exhaustion_refuses_instead_of_foreign_duplicate(tmp_path) -> None:
    """候选全被其它归属占用时：抛明确的冲突异常，绝不返回别人的记录当 Duplicate。"""
    connection = sqlite3.connect(str(tmp_path / "exhaust.db"))
    store = TaskStore(connection)
    store.migrate()
    try:
        foreign, created = store.register_idempotent_task(
            entry="qq", group_id=1, user_id=2, agent="codex", prompt="QQ",
            status="queued", idempotency_key="v1|http|X",  # 跨入口占用基键
        )
        assert created
        foreign_ids = {foreign["task_id"]}
        for index in range(2, 9):  # `#2`..`#8`：同入口方案，但逻辑键各不相同
            row, created = store.register_idempotent_task(
                entry="http", group_id=1, user_id=2, agent="codex", prompt="OTHER",
                status="succeeded", idempotency_key=f"v1|http|X#{index}",
                key_scheme=HTTP_KEY_SCHEME, logical_key=f"v1|http|X#{index}",
            )
            assert created
            foreign_ids.add(row["task_id"])
        assert store.find_owned_by_idempotency(
            entry="http", group_id=1, user_id=2, idempotency_key="v1|http|X",
            key_scheme=HTTP_KEY_SCHEME,
        ) is None
        with pytest.raises(IdempotencyKeyConflict):
            store.register_idempotent_task(
                entry="http", group_id=1, user_id=2, agent="codex", prompt="NEW",
                status="queued", idempotency_key="v1|http|X", key_scheme=HTTP_KEY_SCHEME,
                logical_key="v1|http|X",
            )
        count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 8, count
        assert len(foreign_ids) == 8, foreign_ids
        assert store.find_owned_by_idempotency(
            entry="http", group_id=1, user_id=2, idempotency_key="v1|http|X",
            key_scheme=HTTP_KEY_SCHEME,
        ) is None
    finally:
        connection.close()


async def test_http_variant_exhaustion_is_explicitly_rejected(tmp_path) -> None:
    """HTTP：候选存储位全被其它归属占用时返回明确拒绝，不冒充任何既有任务。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    foreign_ids = set()
    qq_row, created = harness.task_store.register_idempotent_task(
        entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
        prompt="QQ", status="queued", idempotency_key="v1|http|X",
    )
    assert created
    foreign_ids.add(qq_row["task_id"])
    for index in range(2, 9):
        row, created = harness.task_store.register_idempotent_task(
            entry="http", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="codex",
            prompt="OTHER", status="succeeded",
            idempotency_key=f"v1|http|X#{index}", key_scheme=HTTP_KEY_SCHEME,
            logical_key=f"v1|http|X#{index}",
        )
        assert created
        foreign_ids.add(row["task_id"])
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "X"}, json=_BODY,
            )
            assert response.status_code == 400, response.text
            body = response.json()
            assert body.get("task_id") not in foreign_ids, body
            await _drain(app)
        assert len(agent.calls) == 0, agent.calls
        assert len(_rows(harness)) == 8, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_null_scheme_legacy_row_with_prefix_and_hash_is_replayed(tmp_path) -> None:
    """升级前的旧行（scheme/logical 均 NULL）即使原始 key 本身含 `v1|http` 与 `#n`：
    同键重放仍命中旧行；字面相邻的其它 raw key 不得被它吞掉。"""
    import time

    agent = FakeAgent("codex", reply="OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    now = time.time()
    with harness.context_store.connection:
        harness.context_store.connection.execute(
            "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, prompt, status, "
            "idempotency_key, created_at, updated_at) "
            "VALUES (?, 'http', ?, ?, 'codex', 'OLD', 'succeeded', ?, ?, ?)",
            ("legacy0001", ALLOWED_GROUP, ALLOWED_USER, "v1|http|A#2", now, now),
        )
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            replay = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "v1|http|A#2"},
                json={**_BODY, "prompt": "REPLAY_LEGACY"},
            )
            assert replay.status_code == 202, replay.text
            assert replay.json().get("idempotent") is True, replay.json()
            assert replay.json()["task_id"] == "legacy0001", replay.json()
            fresh = await client.post(
                "/api/v1/tasks", headers={**auth(), "Idempotency-Key": "A#2"},
                json={**_BODY, "prompt": "FRESH"},
            )
            assert fresh.status_code == 202, fresh.text
            assert fresh.json().get("idempotent") is not True, fresh.json()
            assert fresh.json()["task_id"] != "legacy0001", fresh.json()
            await _drain(app)
        assert len(agent.calls) == 1, agent.calls
        assert len(_rows(harness)) == 2, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_null_logical_variant_row_replay_is_same_task(tmp_path) -> None:
    """首轮中间实现写下的变体行（logical_key NULL、基键被旧行占用）：重放必须命中同一行。

    `v1|http|A` 被旧原始键行（scheme NULL）占用时，旧变体行落在 `v1|http|A#2`（scheme
    http:v1、无逻辑键记录）。本方案用"基键确实被其它归属占用"的结构证据认领它，
    既不误认领别人的基键，也不让同一 raw key 重放/并发新建第二个任务。
    """
    import time

    agent = FakeAgent("codex", reply="OK")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})
    now = time.time()
    with harness.context_store.connection:
        harness.context_store.connection.execute(
            "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, prompt, status, "
            "idempotency_key, key_scheme, logical_key, created_at, updated_at) "
            "VALUES (?, 'http', ?, ?, 'codex', 'OLD', 'succeeded', ?, NULL, NULL, ?, ?)",
            ("legacyA001", ALLOWED_GROUP, ALLOWED_USER, "v1|http|A", now, now),
        )
        harness.context_store.connection.execute(
            "INSERT INTO tasks (task_id, entry, group_id, user_id, agent, prompt, status, "
            "idempotency_key, key_scheme, logical_key, created_at, updated_at) "
            "VALUES (?, 'http', ?, ?, 'codex', 'ROUND1', 'queued', ?, ?, NULL, ?, ?)",
            ("round1var2", ALLOWED_GROUP, ALLOWED_USER, "v1|http|A#2", HTTP_KEY_SCHEME, now, now),
        )
    app = build_api_app(harness)
    headers = {**auth(), "Idempotency-Key": "A"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/tasks", headers=headers, json=_BODY)
            assert first.status_code == 202, first.text
            assert first.json().get("idempotent") is True, first.json()
            assert first.json()["task_id"] == "round1var2", first.json()
            await _drain(app)
        assert len(agent.calls) == 0, agent.calls
        assert len(_rows(harness)) == 2, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


# ---------- F4（第二轮）：预登记之后的构造/规格异常不得留永久 queued/awaiting ----------


async def test_qq_build_spec_exception_terminalizes_claim_and_keeps_key(tmp_path) -> None:
    """单项：登记后 build_spec 抛异常 → failed/internal_error、保留键、重放不重跑。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})

    def build(*, request, workspace, mode):
        raise RuntimeError("synthetic-spec-error")

    harness.tasks.build_spec = build
    message = _message("/task codex -- X", message_id=602002)
    try:
        await harness.dispatcher.handle_message(message)
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][2] == "failed", rows
        assert rows[0][3] == "internal_error", rows
        assert rows[0][4], rows          # 幂等键保留（同消息重放不重跑）
        assert len(agent.calls) == 0, agent.calls
        await harness.dispatcher.handle_message(message)
        assert len(agent.calls) == 0, agent.calls
        assert len(_rows(harness)) == 1, _rows(harness)
        assert "未重复执行" in harness.transport.texts()[-1], harness.transport.texts()
    finally:
        await harness.aclose()


@pytest.mark.parametrize("terminal", ["cancelled", "interrupted", "succeeded"])
async def test_build_spec_exception_does_not_overwrite_terminal(tmp_path, terminal) -> None:
    """单项：构造异常出口同样只改活动行，绝不覆盖并发取消/中断/成功终态。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})

    def build(*, request, workspace, mode):
        harness.task_store.update_status_if_active(str(request.task_id), terminal)
        raise RuntimeError("synthetic-spec-error")

    harness.tasks.build_spec = build
    try:
        await harness.dispatcher.handle_message(_message("/task codex -- X", message_id=602003))
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][2] == terminal, rows
        assert rows[0][4], rows
        assert len(agent.calls) == 0, agent.calls
    finally:
        await harness.aclose()


async def test_all_build_spec_exception_terminalizes_claims(tmp_path) -> None:
    """`/task all`：登记后 build_spec 抛异常同样收敛 failed，不留永久 queued。"""
    agents = {"glm": FakeAgent("glm"), "codex": FakeAgent("codex")}
    harness = ConsoleHarness(
        make_settings(tmp_path, shared_agent_context=True), agents
    )

    def build(*, request, workspace, mode):
        raise RuntimeError("synthetic-spec-error")

    harness.tasks.build_spec = build
    message = _message("/task all -- X", message_id=603003)
    try:
        await harness.dispatcher.handle_message(message)
        rows = _rows(harness)
        assert rows, rows
        assert all(row[2] == "failed" for row in rows), rows
        assert all(row[3] == "internal_error" for row in rows), rows
        assert all(row[4] for row in rows), rows
        assert len(agents["glm"].calls) == 0 and len(agents["codex"].calls) == 0
        # 重放：已失败的行不重跑；尚未登记的后续 Agent 走同一失败出口，仍不调用模型
        await harness.dispatcher.handle_message(message)
        assert all(row[2] == "failed" for row in _rows(harness)), _rows(harness)
        assert len(agents["glm"].calls) == 0 and len(agents["codex"].calls) == 0
    finally:
        await harness.aclose()


async def test_qq_resume_build_spec_exception_does_not_leave_awaiting(tmp_path) -> None:
    """续跑：目录确认后 build_spec 抛异常 → failed 终态，不留永久 awaiting_selection。"""
    root = tmp_path / "root"
    (root / "alpha-one").mkdir(parents=True)
    (root / "alpha-two").mkdir()
    agent = FakeAgent("codex", reply="SEL_OK")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"codex": agent})
    message = _message("/task codex --mode read --find alpha -- X", message_id=604004)
    try:
        await harness.dispatcher.handle_message(message)
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][2] == "awaiting_selection", rows
        code = _selection_code(harness.transport.texts())

        def build(*, request, workspace, mode):
            raise RuntimeError("synthetic-resume-spec-error")

        harness.tasks.build_spec = build
        await harness.dispatcher.handle_message(make_message(f"/workspace select {code} 1"))
        rows = _rows(harness)
        assert len(rows) == 1 and rows[0][2] == "failed", rows
        assert rows[0][3] == "internal_error", rows
        assert rows[0][4], rows
        assert len(agent.calls) == 0, agent.calls
        # 确认码已消费：不得复活执行、不得留下等待
        await harness.dispatcher.handle_message(make_message(f"/workspace select {code} 1"))
        assert _rows(harness)[0][2] == "failed", _rows(harness)
        assert len(agent.calls) == 0, agent.calls
    finally:
        await harness.aclose()


async def test_http_build_spec_exception_is_terminal_and_replay_idempotent(tmp_path) -> None:
    """HTTP：登记后 build_spec 抛异常 → 行收敛 failed、保留键；同键重放返回同一行且不重跑。"""
    agent = FakeAgent("codex")
    harness = ConsoleHarness(make_settings(tmp_path), {"codex": agent})

    def build(*, request, workspace, mode):
        raise RuntimeError("synthetic-http-spec-error")

    harness.tasks.build_spec = build
    app = build_api_app(harness)
    headers = {**auth(), "Idempotency-Key": "http-spec-raise"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            with pytest.raises(RuntimeError):
                await client.post("/api/v1/tasks", headers=headers, json=_BODY)
            rows = _rows(harness)
            assert len(rows) == 1 and rows[0][2] == "failed", rows
            assert rows[0][3] == "internal_error", rows
            assert rows[0][4], rows
            replay = await client.post("/api/v1/tasks", headers=headers, json=_BODY)
            assert replay.status_code == 202, replay.text
            assert replay.json().get("idempotent") is True, replay.json()
            assert replay.json()["task_id"] == rows[0][0], replay.json()
            await _drain(app)
        assert len(agent.calls) == 0, agent.calls
        assert len(_rows(harness)) == 1, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_resume_build_spec_exception_does_not_leave_awaiting(tmp_path) -> None:
    """HTTP 续跑：目录确认后 build_spec 抛异常同样收敛 failed，不留永久 awaiting_selection。"""
    root = tmp_path / "root"
    (root / "alpha-one").mkdir(parents=True)
    (root / "alpha-two").mkdir()
    agent = FakeAgent("codex", reply="SEL_OK")
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {"codex": agent})
    app = build_api_app(harness)
    headers = {**auth(), "Idempotency-Key": "http-resume-raise"}
    payload = {"agent": "codex", "prompt": "X", "mode": "read", "find": "alpha",
               "group_id": ALLOWED_GROUP}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/tasks", headers=headers, json=payload)
            assert first.status_code == 202, first.text
            assert first.json()["status"] == "awaiting_selection", first.json()
            task_id = first.json()["task_id"]
            code = first.json()["selection_code"]

            def build(*, request, workspace, mode):
                raise RuntimeError("synthetic-resume-spec-error")

            harness.tasks.build_spec = build
            selected = await client.post(
                "/api/v1/workspaces/select", headers=auth(),
                json={"code": code, "index": 1, "group_id": ALLOWED_GROUP},
            )
            assert selected.status_code == 200, selected.text
            await _drain(app)
            rows = _rows(harness)
            assert len(rows) == 1 and rows[0][2] == "failed", rows
            assert rows[0][3] == "internal_error", rows
            assert rows[0][4], rows
            replay = await client.post("/api/v1/tasks", headers=headers, json=payload)
            assert replay.status_code == 202, replay.text
            assert replay.json().get("idempotent") is True, replay.json()
            assert replay.json()["task_id"] == task_id, replay.json()
            await _drain(app)
        assert len(agent.calls) == 0, agent.calls
        assert len(_rows(harness)) == 1, _rows(harness)
    finally:
        await _drain(app)
        await harness.aclose()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
