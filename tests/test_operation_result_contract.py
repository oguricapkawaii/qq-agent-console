"""V01：操作结果只由动作、提交与命令回执定义（QQ/HTTP/team 一致）。

覆盖场景（每一类都要有正反两面，不靠自然语言关键词判定）：
1. 正常修改：模型给出 final 之前真的 propose_changes 并提交 → succeeded + changed；
2. 仅自述：只有 final、没有任何动作 → no_change（既不是失败，也不是"已修改"）；
3. 合法无需修改：同样只有 final，但语义是"无需变更" → no_change（**不得误报失败**）；
4. 提交失败：propose_changes 写出失败 → commit_failed + 保留变更记录；
5. 读/执行证据：read 有读取回执才算完成；exec 有命令回执才算完成；
6. team 上游：no_change 的步骤不得当作"已完成步骤"传给下游；
7. 三入口一致：QQ 文本、HTTP 记录、team 步骤读取同一份 operation/status。
"""

from __future__ import annotations

import json

from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    ScriptedAgent,
    make_settings,
    workspace_settings,
)
from app.backends.protocol import TaskRequest

CHANGE_ACTION = json.dumps({
    "actions": [{"action": "propose_changes", "changes": [
        {"path": "edited.txt", "content": "EXPECTED"}]}],
})


def _edit_request(workspace, prompt: str = "创建 edited.txt"):
    return TaskRequest(
        entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent="zcode",
        prompt=prompt, mode="edit", workspace=workspace, use_chat_context=False,
    )


async def _run(harness, request, *, mode: str):
    spec, error = harness.tasks.build_spec(
        request=request, workspace=request.workspace, mode=mode,
    )
    assert spec is not None, error
    return await harness.tasks.run(request, spec=spec)


# ---------- 1) 正常修改 ----------


async def test_edit_with_committed_change_is_succeeded_and_changed(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    agent = ScriptedAgent("zcode", [CHANGE_ACTION, json.dumps({"final": "已创建 edited.txt"})])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(harness, _edit_request(ws), mode="edit")
        assert outcome.ok and outcome.status == "succeeded"
        assert outcome.operation == "changed"
        assert outcome.action_counts.get("propose_changes") == 1
        assert (ws / "edited.txt").read_text() == "EXPECTED"
        assert [change.status for change in outcome.changes] == ["applied"]
        # 三入口共用同一份取值：as_dict 是 QQ/HTTP 展示与记录的数据源
        assert outcome.as_dict()["operation"] == "changed"
        assert outcome.as_dict()["action_counts"]["propose_changes"] == 1
    finally:
        await harness.aclose()


# ---------- 2) 仅自述（V01 的核心反例） ----------


async def test_edit_final_only_is_no_change_not_success(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    agent = ScriptedAgent("zcode", [json.dumps({"final": "已创建 edited.txt，内容是 EXPECTED。"})])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(harness, _edit_request(ws), mode="edit")
        assert not (ws / "edited.txt").exists()
        assert outcome.status != "succeeded"
        assert outcome.status == "no_change"
        assert outcome.operation == "none"
        assert outcome.changes == []
        # 记录里必须体现"没有实际操作"，且不能被当成失败（合法无需修改也走这条路径）
        assert outcome.ok is True
        assert any("operation none" in line for line in outcome.tool_log), outcome.tool_log
        record = harness.task_store.get_task(outcome.task_id)
        assert record["status"] == "no_change"
        assert record["operation"] == "none"
        assert record["changed_files"] == 0
        assert json.loads(record["action_counts"]) == {}
        # QQ 回复必须明确标注没有修改，不能显示为已完成
    finally:
        await harness.aclose()
    # 另起一套装配走完整 QQ 命令链路（同一模型行为），确认对外文案不显示"已修改"
    qq = ConsoleHarness(
        workspace_settings(tmp_path, root=ws),
        {"zcode": ScriptedAgent("zcode", [json.dumps({
            "final": "已创建 edited.txt，内容是 EXPECTED。"})])},
    )
    try:
        reply = await qq.command(
            f'/task zcode --dir "{ws}" --mode edit -- 创建 {ws / "edited.txt"}'
        )
        assert "无实际操作" in reply, reply
        assert "失败" not in reply, reply
        assert not (ws / "edited.txt").exists()
    finally:
        await qq.aclose()


# ---------- 3) 合法"无需修改"不得误报失败 ----------


async def test_legitimate_no_change_is_not_failure(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "config.txt").write_text("already correct", encoding="utf-8")
    # 模型自己判断"无需修改"，同样只给 final —— 服务端不得把它当失败，也不得当已修改
    agent = ScriptedAgent("zcode", [json.dumps({
        "final": "检查后确认无需变更：config.txt 已经是期望内容。",
    })])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(
            harness, _edit_request(ws, prompt="检查 config.txt，如已正确则不要改动"), mode="edit",
        )
        assert outcome.ok is True, "合法无需修改不能被判为失败"
        assert outcome.status == "no_change"
        assert outcome.operation == "none"
        assert outcome.error is None
        assert outcome.changes == []
        # 失败任务的文案不应出现（QQ 侧只标注"无实际操作"）
    finally:
        await harness.aclose()
    qq = ConsoleHarness(
        workspace_settings(tmp_path, root=ws),
        {"zcode": ScriptedAgent("zcode", [json.dumps({
            "final": "检查后确认无需变更：config.txt 已经是期望内容。"})])},
    )
    try:
        reply = await qq.command(
            f'/task zcode --dir "{ws}" --mode edit -- 检查 config.txt，如已正确则不要改动'
        )
        assert "失败" not in reply, reply
        assert "无实际操作" in reply, reply
        assert (ws / "config.txt").read_text() == "already correct"
    finally:
        await qq.aclose()


# ---------- 4) 提交失败 ----------


async def test_edit_commit_failure_keeps_records_and_fails(tmp_path, monkeypatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    agent = ScriptedAgent("zcode", [CHANGE_ACTION, json.dumps({"final": "写好了"})])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    from app.workspaces import changes as changes_module

    def broken_apply_write(self, change, root):
        raise OSError("disk full (fixture)")

    monkeypatch.setattr(changes_module.ChangeSet, "_apply_write", broken_apply_write, raising=True)
    try:
        outcome = await _run(harness, _edit_request(ws), mode="edit")
        assert not outcome.ok
        assert outcome.status == "failed"
        assert outcome.error == "commit_failed"
        assert outcome.changes, "提交失败必须保留变更记录（含失败原因）"
        assert all(change.status != "applied" for change in outcome.changes)
        assert not (ws / "edited.txt").exists()
    finally:
        await harness.aclose()


# ---------- 5) 读/执行证据 ----------


async def test_read_without_any_action_is_no_change(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    agent = ScriptedAgent("zcode", [json.dumps({"final": "文件内容是 hello（其实我没读）"})])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(
            harness,
            TaskRequest(entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
                        agent="zcode", prompt="读 a.txt", mode="read", workspace=ws,
                        use_chat_context=False),
            mode="read",
        )
        assert outcome.status == "no_change"
        assert outcome.operation == "none"
        assert outcome.ok is True
    finally:
        await harness.aclose()


async def test_read_with_action_is_succeeded_observed(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    agent = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "内容是 hello"}),
    ])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(
            harness,
            TaskRequest(entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
                        agent="zcode", prompt="读 a.txt", mode="read", workspace=ws,
                        use_chat_context=False),
            mode="read",
        )
        assert outcome.ok and outcome.status == "succeeded"
        assert outcome.operation == "observed"
        assert outcome.action_counts.get("read") == 1
    finally:
        await harness.aclose()


async def test_exec_without_command_is_no_change(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    agent = ScriptedAgent("zcode", [json.dumps({"final": "命令已执行（其实没有）"})])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(
            harness,
            TaskRequest(entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
                        agent="zcode", prompt="跑一条命令", mode="exec", workspace=ws,
                        use_chat_context=False),
            mode="exec",
        )
        assert outcome.status == "no_change"
        assert outcome.operation == "none"
        assert outcome.action_counts.get("run_command", 0) == 0
    finally:
        await harness.aclose()


# ---------- 6) team 上游结果处理 ----------


async def test_team_step_without_operation_is_not_reported_as_completed(tmp_path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "source.txt").write_text("data", encoding="utf-8")
    agent = ScriptedAgent("deepseek", [json.dumps({"final": "读完了"})])  # 只有自述
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"deepseek": agent})
    try:
        team = harness.create_team("无操作步骤")
        harness.add_step(team, "s1", "deepseek", "读", mode="read", workspace=str(ws))
        harness.add_step(team, "s2", "deepseek", "汇总", mode="text",
                         workspace=None, after=("s1",))
        await harness.command(f"/team run {team}")
        status = await harness.wait_team(team)
        assert status == "failed", "没有任何实际操作的上游步骤不能算完成"
        step = harness.workflow_store.get_step_run(team, 1, "s1")
        assert step.status == "failed"
        assert step.error == "no_operation"
        downstream = harness.workflow_store.get_step_run(team, 1, "s2")
        assert downstream.status == "blocked"
    finally:
        await harness.aclose()


# ---------- 7) HTTP 入口一致性 ----------


async def test_http_status_and_result_expose_no_change(tmp_path) -> None:
    import httpx

    from test_api_v1 import TOKEN, auth, build_api_app

    ws = tmp_path / "workspace"
    ws.mkdir()
    agent = ScriptedAgent("zcode", [json.dumps({"final": "已创建 edited.txt（未执行动作）"})])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    app = build_api_app(harness)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            created = await http.post("/api/v1/tasks", json={
                "agent": "zcode", "prompt": "创建 edited.txt", "mode": "edit",
                "dir": str(ws), "group_id": ALLOWED_GROUP,
            }, headers=auth())
            assert created.status_code == 202, created.text
            task_id = created.json()["task_id"]
            import asyncio
            for _ in range(400):
                detail = await http.get(f"/api/v1/tasks/{task_id}", headers=auth())
                if detail.json()["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.01)
            body = detail.json()
            assert body["status"] == "no_change", body
            ops = await http.get(f"/api/v1/tasks/{task_id}/result", headers=auth())
            payload = ops.json()
            assert payload["operation"] == "none", payload
            assert payload["action_counts"] in ({}, None)
            assert body["operation"] == "none"
    finally:
        await harness.aclose()


# ---------- R62：只读回执不能证明 edit/exec 完成 ----------


async def test_read_then_fake_edit_is_no_change(tmp_path) -> None:
    """先真的 read，再 final 谎称创建文件 → 必须 no_change（不能被当成已修改）。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "source.txt").write_text("BEFORE", encoding="utf-8")
    agent = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "read", "path": "source.txt"}]}),
        json.dumps({"final": "已创建 edited.txt，内容是 EXPECTED。"}),
    ])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(harness, _edit_request(ws), mode="edit")
        assert outcome.status == "no_change", (outcome.status, outcome.operation)
        assert outcome.operation == "none"
        assert not (ws / "edited.txt").exists()
        # 只读回执仍然如实保留（读过什么没有被抹掉）
        assert outcome.action_counts.get("read") == 1
        assert any("broker:read" in line for line in outcome.tool_log)
        # 说明必须点出"只完成了只读动作、没完成本模式要求"
        notice = "\n".join(outcome.tool_log)
        assert "没有" in notice and "模式 edit" in notice, notice
    finally:
        await harness.aclose()


async def test_list_then_fake_edit_is_no_change(tmp_path) -> None:
    """list/search 回执同样不能证明 edit 完成。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("data", encoding="utf-8")
    agent = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "list", "path": ".", "depth": 1}]}),
        json.dumps({"actions": [{"action": "search", "query": "data"}]}),
        json.dumps({"final": "已按要求修改文件。"}),
    ])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(harness, _edit_request(ws), mode="edit")
        assert outcome.status == "no_change"
        assert outcome.operation == "none"
        assert outcome.action_counts.get("list") == 1
        assert outcome.action_counts.get("search") == 1
    finally:
        await harness.aclose()


async def test_read_then_fake_exec_is_no_change(tmp_path) -> None:
    """read 回执不能证明 exec 完成；只有 run_command 回执才算。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("data", encoding="utf-8")
    agent = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "read", "path": "a.txt"}]}),
        json.dumps({"final": "命令已执行完成。"}),
    ])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(
            harness,
            TaskRequest(entry="qq", group_id=ALLOWED_GROUP, user_id=ALLOWED_USER,
                        agent="zcode", prompt="跑一条命令", mode="exec", workspace=ws,
                        use_chat_context=False),
            mode="exec",
        )
        assert outcome.status == "no_change"
        assert outcome.operation == "none"
        assert outcome.action_counts.get("run_command", 0) == 0
    finally:
        await harness.aclose()


async def test_edit_mode_read_then_commit_still_succeeds(tmp_path) -> None:
    """正常 edit（先读后提交）仍然必须成功：只读回执不会掩盖真实提交。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "source.txt").write_text("BEFORE", encoding="utf-8")
    agent = ScriptedAgent("zcode", [
        json.dumps({"actions": [{"action": "read", "path": "source.txt"}]}),
        CHANGE_ACTION,
        json.dumps({"final": "已创建 edited.txt"}),
    ])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"zcode": agent})
    try:
        outcome = await _run(harness, _edit_request(ws), mode="edit")
        assert outcome.ok and outcome.status == "succeeded"
        assert outcome.operation == "changed"
        assert outcome.action_counts.get("read") == 1
        assert outcome.action_counts.get("propose_changes") == 1
        assert (ws / "edited.txt").read_text() == "EXPECTED"
    finally:
        await harness.aclose()


async def test_team_read_then_fake_edit_step_fails_and_blocks_downstream(tmp_path) -> None:
    """team：read→假称修改的 edit 步骤必须失败，下游必须 blocked（R62 复用约束）。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "source.txt").write_text("data", encoding="utf-8")
    agent = ScriptedAgent("deepseek", [
        json.dumps({"actions": [{"action": "read", "path": "source.txt"}]}),
        json.dumps({"final": "已创建 result.txt。"}),
    ])
    harness = ConsoleHarness(workspace_settings(tmp_path, root=ws), {"deepseek": agent})
    try:
        team = harness.create_team("先读后假称修改")
        harness.add_step(team, "edit", "deepseek", "创建 result.txt", mode="edit",
                         workspace=str(ws))
        harness.add_step(team, "next", "deepseek", "汇总", mode="text", after=("edit",))
        await harness.command(f"/team run {team}")
        assert await harness.wait_team(team) == "failed"
        step = harness.workflow_store.get_step_run(team, 1, "edit")
        assert step.status == "failed" and step.error == "no_operation"
        assert harness.workflow_store.get_step_run(team, 1, "next").status == "blocked"
        assert not (ws / "result.txt").exists()
    finally:
        await harness.aclose()


async def test_mode_aware_operation_kind_unit(tmp_path) -> None:
    """直接对判定函数做模式化断言（无模型、无文件系统）。"""
    from app.backends.broker_loop import (
        BrokerSessionResult,
        OPERATION_CHANGED,
        OPERATION_EXECUTED,
        OPERATION_NONE,
        OPERATION_OBSERVED,
    )

    class Commit:
        status = "applied"

    reads = BrokerSessionResult(ok=True, action_counts={"read": 3, "search": 1})
    commands = BrokerSessionResult(ok=True, action_counts={"run_command": 1})
    both = BrokerSessionResult(ok=True, action_counts={"read": 1, "run_command": 2})
    nothing = BrokerSessionResult(ok=True, action_counts={})

    assert reads.operation_kind(mode="read") == OPERATION_OBSERVED
    assert reads.operation_kind(mode="edit") == OPERATION_NONE
    assert reads.operation_kind(mode="exec") == OPERATION_NONE
    assert commands.operation_kind(mode="exec") == OPERATION_EXECUTED
    assert commands.operation_kind(mode="edit") == OPERATION_NONE
    assert both.operation_kind(mode="exec") == OPERATION_EXECUTED
    assert nothing.operation_kind(mode="text") == OPERATION_OBSERVED
    assert reads.operation_kind(mode="edit", commits=[Commit()]) == OPERATION_CHANGED
    assert nothing.operation_kind(mode="edit", commits=[Commit()]) == OPERATION_CHANGED


# ---------- R61：根内敏感拒绝清单（与公共清单同源） ----------


def test_workspace_sensitive_deny_paths_covers_nested_forms(tmp_path) -> None:
    from app.agents.codex import read_only_permission_config, workspace_sensitive_deny_paths

    ws = tmp_path / "workspace"
    (ws / "sub" / "deep").mkdir(parents=True)
    (ws / "creds").mkdir()
    (ws / "runtime").mkdir()
    (ws / ".ssh").mkdir()
    (ws / ".env").write_text("x", encoding="utf-8")
    (ws / ".env.production").write_text("x", encoding="utf-8")
    (ws / "sub" / ".env").write_text("x", encoding="utf-8")
    (ws / "sub" / "deep" / ".env.local").write_text("x", encoding="utf-8")
    (ws / "creds" / "id_rsa").write_text("x", encoding="utf-8")
    (ws / "creds" / "server.pem").write_text("x", encoding="utf-8")
    (ws / "data").mkdir()
    (ws / "data" / "bot.db").write_text("x", encoding="utf-8")
    (ws / "runtime" / "state.json").write_text("x", encoding="utf-8")
    (ws / ".ssh" / "id_rsa").write_text("x", encoding="utf-8")
    (ws / "readme.md").write_text("ok", encoding="utf-8")
    (ws / "sub" / "main.py").write_text("ok", encoding="utf-8")

    deny = workspace_sensitive_deny_paths(ws)
    joined = "\n".join(deny)
    # 目录级 deny 覆盖其下全部内容（此处 data 目录名命中公共清单 SENSITIVE_NAMES），
    # 因此断言到目录本身即可。
    for expected in (".env", ".env.production", "sub/.env", "sub/deep/.env.local",
                     "creds/id_rsa", "creds/server.pem", "data",
                     "runtime", ".ssh"):
        assert str(ws / expected) in joined, expected
    # 普通文件不得进入 deny 清单（不能靠"整个工作区都拒绝"转绿）
    assert str(ws / "readme.md") not in joined
    assert str(ws / "sub/main.py") not in joined
    # 清单全是绝对路径（CLI 只接受绝对路径 / ~/ / : 作用域）
    assert all(item.startswith("/") for item in deny)

    config = read_only_permission_config(ws)
    assert ":workspace_roots\"=\"read" in config
    assert str(ws / ".env") in config


def test_native_write_profile_also_uses_absolute_denies(tmp_path) -> None:
    """可写 profile 同样只能是绝对 deny（相对/通配会被 CLI 判配置错误）。"""
    from app.agents.codex import permission_config

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".env").write_text("x", encoding="utf-8")
    config = permission_config(ws)
    assert ":workspace_roots\"=\"write" in config
    assert str(ws / ".env") in config
    for bogus in ("|**/.env|", "|.env=|", "permissions.qq_files={\"extends\"=\":workspace\", \"filesystem\"={\".env\"="):
        assert bogus not in config


def test_run_contract_refuses_mismatched_workspace_config(tmp_path) -> None:
    """证据与工作区不一致时，命令级验证必须直接判失败（R63 的误用防线）。"""
    from app.agents.codex import _run_contract, read_only_permission_config

    other = tmp_path / "workspace"
    other.mkdir()
    row = _run_contract("/bin/echo", profile="qq_ro",
                        config=read_only_permission_config(tmp_path / "elsewhere"),
                        workspace=other, argv=["/bin/echo", "hi"], timeout=5)
    assert row.get("config_mismatch") is True
    assert row["exit_code"] is None
    assert "未覆盖本次工作区" in row["stderr"]
