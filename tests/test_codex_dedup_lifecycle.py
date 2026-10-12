import asyncio
import dataclasses
import json
from helpers import ConsoleHarness, FakeAgent, make_message, make_settings, ALLOWED_GROUP, ALLOWED_USER, BOT_SELF, workspace_settings
from app.transports.onebot_ws import OneBotWebSocketTransport


async def test_selector_rejection_does_not_leave_queued_reservation(tmp_path):
    agent=FakeAgent('codex')
    harness=ConsoleHarness(make_settings(tmp_path),{'codex':agent})
    try:
        message=dataclasses.replace(make_message('/task codex --mode read --find alpha -- X'),message_id=434343)
        await harness.dispatcher.handle_message(message)
        rows=harness.context_store.connection.execute('SELECT status,error FROM tasks').fetchall()
        assert len(agent.calls)==0
        assert all(row[0] in ('failed','rejected') for row in rows),{'rows':rows,'reply':harness.transport.texts()[-1]}
    finally:
        await harness.aclose()


async def test_broadcast_spec_rejection_does_not_leave_queued_reservation(tmp_path):
    agent=FakeAgent('codex')
    harness=ConsoleHarness(make_settings(tmp_path,console_agents={'codex':{'default_model':'not-a-valid-model'}}),{'codex':agent})
    try:
        # 实际登记过的 Agent 设置默认模型为不存在的选项，模拟配置/目录变更后的拒绝。
        harness.backends.agent('codex').default_model='not-a-valid-model'
        message=dataclasses.replace(make_message('/task all -- X'),message_id=444444)
        await harness.dispatcher.handle_message(message)
        rows=harness.context_store.connection.execute('SELECT status,error FROM tasks').fetchall()
        assert len(agent.calls)==0
        assert all(row[0] in ('failed','rejected') for row in rows),rows
    finally:
        await harness.aclose()


async def test_qq_missing_message_id_keeps_compatibility(tmp_path):
    agent=FakeAgent('codex')
    harness=ConsoleHarness(make_settings(tmp_path),{'codex':agent})
    try:
        for _ in range(2):
            await harness.dispatcher.handle_message(make_message('/task codex -- X'))
        assert len(agent.calls)==2
    finally:
        await harness.aclose()


async def test_transport_latest_identity_does_not_change_old_event_key(tmp_path):
    agent=FakeAgent('codex')
    harness=ConsoleHarness(make_settings(tmp_path),{'codex':agent})
    transport=OneBotWebSocketTransport(ws_url='ws://unused',on_event=harness.dispatcher.handle_onebot_event)
    harness.dispatcher.transport=transport
    # 仅替换发送为假传输，真实WebSocket从未启动。
    transport.send_group_message=harness.transport.send_group_message
    def event(bot,mid):
        return {'post_type':'message','message_type':'group','group_id':ALLOWED_GROUP,'user_id':ALLOWED_USER,'self_id':bot,'message_id':mid,'message':'/task codex -- X'}
    try:
        # 先排入旧身份事件，再排入另一身份事件，尚未让事件处理任务运行。
        first=event(BOT_SELF,454545)
        transport._handle_raw(json.dumps(first))
        transport._handle_raw(json.dumps(event(BOT_SELF+1,464646)))
        await asyncio.gather(*list(transport._event_tasks))
        calls_before=len(agent.calls)
        transport._handle_raw(json.dumps(first))
        await asyncio.gather(*list(transport._event_tasks))
        assert len(agent.calls)==calls_before,{'before':calls_before,'after':len(agent.calls)}
    finally:
        await harness.aclose()


async def test_http_legacy_raw_key_row_is_still_deduplicated(tmp_path):
    """F2 兼容：历史 HTTP 行存的是**未加前缀**的原始 key，重放时必须命中既有任务。"""
    import httpx
    from test_api_v1 import build_api_app, auth, _drain
    from helpers import ALLOWED_GROUP

    agent = FakeAgent('codex', reply='LEGACY_OK')
    harness = ConsoleHarness(make_settings(tmp_path), {'codex': agent})
    # 模拟历史记录：直接写一行 entry=http、key=原始值（无 v1|http| 前缀）
    legacy, created = harness.task_store.register_idempotent_task(
        entry='http', group_id=ALLOWED_GROUP, user_id=ALLOWED_USER, agent='codex',
        prompt='OLD', mode='text', status='succeeded',
        idempotency_key='legacy-raw-key',
    )
    assert created
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://test') as client:
            response = await client.post(
                '/api/v1/tasks', headers={**auth(), 'Idempotency-Key': 'legacy-raw-key'},
                json={'agent': 'codex', 'prompt': 'NEW_BODY', 'group_id': ALLOWED_GROUP},
            )
            assert response.status_code == 202, response.text
            body = response.json()
            assert body.get('idempotent') is True, body
            assert body['task_id'] == legacy['task_id'], body
            await _drain(app)
        assert len(agent.calls) == 0, '兼容路径不得重新执行'
        count = harness.context_store.connection.execute(
            'SELECT COUNT(*) FROM tasks').fetchone()[0]
        assert count == 1, count
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_pending_selection_resume_executes_once(tmp_path):
    """F1：目录选择后按原请求续跑**一次**；同一 key 重放不再产生新任务。"""
    import httpx
    from test_api_v1 import build_api_app, auth, _drain
    from helpers import ALLOWED_GROUP

    agent = FakeAgent('codex', reply='RESUMED_OK')
    root = tmp_path / 'root'
    (root / 'alpha-one').mkdir(parents=True)
    (root / 'alpha-two').mkdir()
    harness = ConsoleHarness(workspace_settings(tmp_path, root=root), {'codex': agent})
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://test') as client:
            headers = {**auth(), 'Idempotency-Key': 'resume-key'}
            payload = {'agent': 'codex', 'prompt': 'X', 'mode': 'read', 'find': 'alpha',
                       'group_id': ALLOWED_GROUP}
            first = await client.post('/api/v1/tasks', headers=headers, json=payload)
            assert first.json()['status'] == 'awaiting_selection', first.json()
            task_id = first.json()['task_id']
            code = first.json()['selection_code']
            # 候选序号从 1 开始；先取候选数，再选第一个有效序号
            candidates = first.json().get('candidates') or []
            assert candidates, first.json()
            selected = await client.post('/api/v1/workspaces/select', headers=auth(),
                                         json={'code': code, 'index': 1,
                                               'group_id': ALLOWED_GROUP})
            assert selected.status_code == 200, selected.text
            await _drain(app)
            calls_after_resume = len(agent.calls)
            assert calls_after_resume >= 1, calls_after_resume
            again = await client.post('/api/v1/tasks', headers=headers, json=payload)
            assert again.json()['task_id'] == task_id, again.json()
            await _drain(app)
        # 重放**不得**再次执行（read 模式用 broker 动作协议，单次任务可能多次调用模型，
        # 因此断言"调用次数不再增长"，而不是断言恰好等于 1）
        assert len(agent.calls) == calls_after_resume, (calls_after_resume, len(agent.calls))
        rows = harness.context_store.connection.execute(
            'SELECT task_id, status FROM tasks').fetchall()
        assert len(rows) == 1 and rows[0][0] == task_id, rows
    finally:
        await _drain(app)
        await harness.aclose()


async def test_rejected_terminal_reservation_is_not_rerun_on_replay(tmp_path):
    """F4：拒绝终态后同一消息重放**不再自动执行**（不重跑、不新增任务）。"""
    agent = FakeAgent('codex')
    harness = ConsoleHarness(make_settings(tmp_path), {'codex': agent})
    try:
        message = dataclasses.replace(
            make_message('/task codex --mode read --find alpha -- X'), message_id=454545)
        await harness.dispatcher.handle_message(message)
        rows = harness.context_store.connection.execute(
            'SELECT task_id, status, idempotency_key FROM tasks').fetchall()
        assert len(rows) == 1 and rows[0][1] in ('failed', 'rejected'), rows
        assert rows[0][2], rows          # 幂等键必须保留
        await harness.dispatcher.handle_message(message)
        after = harness.context_store.connection.execute(
            'SELECT task_id, status FROM tasks').fetchall()
        assert after == [(rows[0][0], rows[0][1])], after
        assert len(agent.calls) == 0, len(agent.calls)
    finally:
        await harness.aclose()
