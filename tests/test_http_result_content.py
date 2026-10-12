"""L02：成功任务的正文必须真正持久化并能通过 HTTP 结果接口读回。

真实联调发现：`TaskStore.update_task` 接受 `result_text` 却漏在 UPDATE 的 SET 列表里，
于是 `tasks.result_text` 永远为空，HTTP `/tasks/{id}/result` 返回 `status=succeeded` + `content=""`。

本文件是**持久化 + HTTP** 两个层面的回归，覆盖：
- 存储 round-trip（含数据库重新打开后的读取）；
- text/read/edit/exec 四种模式的 HTTP 结果正文断言；
- 无 Token 与跨用户访问被拒绝；
- 幂等重发仍返回同一 task_id 且不重复调用模型。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.commands.options import MODE_EDIT, MODE_EXEC, MODE_READ, MODE_TEXT
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    OTHER_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    workspace_settings,
)
from test_api_v1 import auth, build_api_app, client, OTHER_TOKEN, TOKEN  # noqa: F401


async def _drain(app) -> None:
    import asyncio

    if getattr(app.state, "background", None):
        await asyncio.gather(*list(app.state.background), return_exceptions=True)


# ---------- 存储 round-trip ----------


def test_store_round_trip_persists_result_text(tmp_path) -> None:
    """最直接的回归：update_task 必须真的把 result_text 写进库。"""
    from app.storage import ContextStore
    from app.tasks.store import TaskStore

    store_ctx = ContextStore(str(tmp_path / "round.db"))
    store = TaskStore(store_ctx.connection)
    store.migrate()
    try:
        created = store.create_task(
            task_id="t-round", entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
            agent="deepseek", backend="deepseek", model="m", mode=MODE_TEXT,
            workspace="", prompt="p", status="running",
        )
        assert created
        assert store.update_task("t-round", status="succeeded", result_text="R1 正文")
        record = store.get_task("t-round")
        assert record["result_text"] == "R1 正文"
        # 二次更新覆盖而不是追加
        assert store.update_task("t-round", result_text="R2 正文")
        assert store.get_task("t-round")["result_text"] == "R2 正文"
    finally:
        store_ctx.close()


def test_result_text_survives_database_reopen(tmp_path) -> None:
    """进程重新打开同一个数据库文件后仍能读到正文（真实部署会重启）。"""
    from app.storage import ContextStore
    from app.tasks.store import TaskStore

    path = str(tmp_path / "reopen.db")
    first = ContextStore(path)
    try:
        store = TaskStore(first.connection)
        store.migrate()
        store.create_task(
            task_id="t-reopen", entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
            agent="deepseek", backend="deepseek", model="m", mode=MODE_TEXT,
            workspace="", prompt="p", status="running",
        )
        store.update_task("t-reopen", status="succeeded", result_text="跨进程正文")
    finally:
        first.close()

    second = ContextStore(path)
    try:
        store = TaskStore(second.connection)
        store.migrate()
        assert store.get_task("t-reopen")["result_text"] == "跨进程正文"
        assert store.list_recent(
            group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, limit=5,
        )[0]["result_text"] == "跨进程正文"
    finally:
        second.close()


# ---------- HTTP：四种模式的正文 ----------


@pytest.mark.parametrize(
    ("mode", "script", "sentinel", "verify"),
    [
        (MODE_TEXT, ["HTTP_TEXT_ANSWER"], "HTTP_TEXT_ANSWER", None),
        (
            MODE_READ,
            [
                json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
                json.dumps({"final": "HTTP_READ_ANSWER"}),
            ],
            "HTTP_READ_ANSWER",
            None,
        ),
        (
            MODE_EDIT,
            [
                json.dumps({"actions": [{"action": "propose_changes", "changes": [
                    {"path": "a.txt", "content": "edited"}]}]}),
                json.dumps({"final": "HTTP_EDIT_ANSWER"}),
            ],
            "HTTP_EDIT_ANSWER",
            "edited",
        ),
        (
            MODE_EXEC,
            [
                json.dumps({"actions": [{"action": "run_command", "argv": [
                    "python3", "-c", "print('exec-ok')"], "timeout": 10}]}),
                json.dumps({"final": "HTTP_EXEC_ANSWER"}),
            ],
            "HTTP_EXEC_ANSWER",
            None,
        ),
    ],
)
async def test_http_result_content_per_mode(tmp_path, mode, script, sentinel, verify) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("original", encoding="utf-8")
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(settings, {"deepseek": ScriptedAgent("deepseek", script)})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            payload = {"agent": "deepseek", "prompt": "do it", "mode": mode}
            if mode != MODE_TEXT:
                payload["dir"] = str(root)
            submitted = await http.post("/api/v1/tasks", json=payload, headers=auth())
            assert submitted.status_code == 202, submitted.text
            task_id = submitted.json()["task_id"]
            await _drain(app)
            result = await http.get(f"/api/v1/tasks/{task_id}/result", headers=auth())
            body = result.json()
            assert body["status"] == "succeeded", body
            assert body["content"] == sentinel, body
            # 存储层同样必须有正文（不是只在内存里）
            assert harness.task_store.get_task(task_id)["result_text"] == sentinel
            if verify is not None:
                assert (root / "a.txt").read_text(encoding="utf-8") == verify
    finally:
        await harness.aclose()


async def test_http_result_content_rejects_missing_and_foreign_token(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    harness = ConsoleHarness(
        settings, {"deepseek": FakeAgent("deepseek", reply=json.dumps({"final": "SECRET_BODY"}))}
    )
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            submitted = await http.post(
                "/api/v1/tasks",
                json={"agent": "deepseek", "prompt": "hi", "mode": MODE_TEXT},
                headers=auth(),
            )
            task_id = submitted.json()["task_id"]
            await _drain(app)
            no_token = await http.get(f"/api/v1/tasks/{task_id}/result")
            assert no_token.status_code == 401
            foreign = await http.get(f"/api/v1/tasks/{task_id}/result", headers=auth(OTHER_TOKEN))
            assert foreign.status_code == 404
            # 被拒绝的响应里不得泄漏正文
            assert "SECRET_BODY" not in no_token.text and "SECRET_BODY" not in foreign.text
    finally:
        await harness.aclose()


async def test_idempotent_resubmit_keeps_content_and_single_model_call(tmp_path) -> None:
    """幂等重发返回同一 task_id、正文不变，且不重复调用模型。"""
    root = tmp_path / "proj"
    root.mkdir()
    settings = workspace_settings(tmp_path, root=root)
    # text 模式下模型直接返回正文（不走 broker 动作协议）
    agent = FakeAgent("deepseek", reply="IDEMPOTENT_BODY")
    harness = ConsoleHarness(settings, {"deepseek": agent})
    app = build_api_app(harness)
    try:
        async with client(app) as http:
            headers = {**auth(), "Idempotency-Key": "same-key"}
            payload = {"agent": "deepseek", "prompt": "hi", "mode": MODE_TEXT}
            first = await http.post("/api/v1/tasks", json=payload, headers=headers)
            task_id = first.json()["task_id"]
            await _drain(app)
            again = await http.post("/api/v1/tasks", json=payload, headers=headers)
            assert again.json()["task_id"] == task_id
            assert again.json().get("idempotent") is True
            await _drain(app)
            result = await http.get(f"/api/v1/tasks/{task_id}/result", headers=auth())
            assert result.json()["content"] == "IDEMPOTENT_BODY"
            assert len(agent.calls) == 1, f"幂等重发重复调用了模型：{len(agent.calls)}"
    finally:
        await harness.aclose()


def test_http_result_content_comes_from_redacted_result(tmp_path) -> None:
    """正文入库前已经过脱敏（与任务记录一致），不额外引入新的泄漏面。"""
    from app.security.redaction import SecretRedactor

    redactor = SecretRedactor()
    redactor.register("sk-livekey123456")
    assert "sk-livekey" not in redactor.redact("answer sk-livekey123456 end")
