import asyncio
import dataclasses
import httpx
from helpers import ConsoleHarness, FakeAgent, ALLOWED_GROUP, ALLOWED_USER, make_message, make_settings, workspace_settings
from test_api_v1 import build_api_app, auth, _drain
from app.routing.qq_dedup import build_key


async def test_http_pending_selection_keeps_idempotency_key(tmp_path):
    root=tmp_path/'root'
    (root/'alpha-one').mkdir(parents=True)
    (root/'alpha-two').mkdir()
    harness=ConsoleHarness(workspace_settings(tmp_path,root=root),{'codex':FakeAgent('codex')})
    app=build_api_app(harness)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={**auth(),'Idempotency-Key':'pending-test-key'}
            payload={'agent':'codex','prompt':'X','mode':'read','find':'alpha','group_id':ALLOWED_GROUP}
            first=await client.post('/api/v1/tasks',headers=headers,json=payload)
            second=await client.post('/api/v1/tasks',headers=headers,json=payload)
            assert first.status_code==second.status_code==202
            assert first.json()['status']=='awaiting_selection',first.json()
            assert first.json()['task_id']==second.json()['task_id'],{'first':first.json()['task_id'],'second':second.json()['task_id']}
            assert harness.context_store.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
    finally:
        await _drain(app)
        await harness.aclose()


async def test_http_raw_key_cannot_collide_with_qq_namespace(tmp_path):
    agent=FakeAgent('codex',reply='OK')
    harness=ConsoleHarness(make_settings(tmp_path),{'codex':agent})
    app=build_api_app(harness)
    try:
        message=dataclasses.replace(make_message('/task codex -- QQ_FIRST'),message_id=424242)
        await harness.dispatcher.handle_message(message)
        raw_key=build_key(message,target='task',agent='codex').value
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/api/v1/tasks',headers={**auth(),'Idempotency-Key':raw_key},json={'agent':'codex','prompt':'HTTP_DIFFERENT','group_id':ALLOWED_GROUP})
            assert response.status_code==202
            await _drain(app)
            assert harness.context_store.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==2,response.json()
            assert len(agent.calls)==2
    finally:
        await _drain(app)
        await harness.aclose()


async def test_all_duplicate_reply_explains_no_reexecution(tmp_path):
    agent=FakeAgent('codex',reply='FIRST_RESULT')
    harness=ConsoleHarness(make_settings(tmp_path),{'codex':agent})
    try:
        message=dataclasses.replace(make_message('/task all -- X'),message_id=424242)
        await harness.dispatcher.handle_message(message)
        start=harness.send_count()
        await harness.dispatcher.handle_message(message)
        assert len(agent.calls)==1
        replies=harness.texts_since(start)
        assert any('未重复执行' in reply for reply in replies),replies
    finally:
        await harness.aclose()


async def test_qq_real_awaiting_selection_branch_is_deduplicated(tmp_path):
    root=tmp_path/'root'
    (root/'alpha-one').mkdir(parents=True)
    (root/'alpha-two').mkdir()
    agent=FakeAgent('codex')
    harness=ConsoleHarness(workspace_settings(tmp_path,root=root),{'codex':agent})
    try:
        message=dataclasses.replace(make_message('/task codex --mode read --find alpha -- X'),message_id=424242)
        await harness.dispatcher.handle_message(message)
        first=harness.context_store.connection.execute('SELECT task_id,status FROM tasks').fetchall()
        assert len(first)==1 and first[0][1]=='awaiting_selection',first
        await harness.dispatcher.handle_message(message)
        after=harness.context_store.connection.execute('SELECT task_id,status FROM tasks').fetchall()
        assert after==first
        assert len(agent.calls)==0
        assert '未重复执行' in harness.transport.texts()[-1]
    finally:
        await harness.aclose()
