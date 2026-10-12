import dataclasses
import httpx
import pytest
from helpers import ConsoleHarness, FakeAgent, make_settings, make_message, ALLOWED_GROUP
from test_api_v1 import build_api_app, auth, _drain


@pytest.mark.parametrize('raw_keys', [('A', 'v1|http|A', 'v1|http|v1|http|A'), ('A#2', 'A', 'A#3')])
async def test_http_prefix_literal_keys_are_distinct(tmp_path, raw_keys):
    agent = FakeAgent('codex', reply='OK')
    harness = ConsoleHarness(make_settings(tmp_path), {'codex': agent})
    app = build_api_app(harness)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            ids = []
            for key in raw_keys:
                r = await client.post('/api/v1/tasks', headers={**auth(), 'Idempotency-Key': key}, json={'agent':'codex','prompt':key,'group_id':ALLOWED_GROUP})
                assert r.status_code == 202, r.text
                ids.append(r.json()['task_id'])
                await _drain(app)
            assert len(set(ids)) == 3, ids
            assert len(agent.calls) == 3
            for key, task_id in zip(raw_keys, ids):
                r = await client.post('/api/v1/tasks', headers={**auth(), 'Idempotency-Key':key}, json={'agent':'codex','prompt':key,'group_id':ALLOWED_GROUP})
                assert r.json()['task_id'] == task_id
            await _drain(app)
            assert len(agent.calls) == 3
    finally:
        await _drain(app)
        await harness.aclose()


@pytest.mark.parametrize('terminal', ['cancelled', 'interrupted', 'succeeded'])
async def test_post_claim_error_preserves_terminal(tmp_path, terminal):
    agent = FakeAgent('codex')
    harness = ConsoleHarness(make_settings(tmp_path), {'codex': agent})
    def resolve(**kwargs):
        harness.tasks._store.update_task(kwargs['task_id'], status=terminal)
        raise RuntimeError('synthetic-resolution-error')
    harness.workspaces.resolve_selector = resolve
    try:
        msg = dataclasses.replace(make_message('/task codex -- X'), message_id=997711)
        await harness.dispatcher.handle_message(msg)
        rows = harness.context_store.connection.execute('SELECT status FROM tasks').fetchall()
        assert [row[0] for row in rows] == [terminal]
        await harness.dispatcher.handle_message(msg)
        assert len(agent.calls) == 0
    finally:
        await harness.aclose()


async def test_build_spec_exception_terminalizes_claim(tmp_path):
    agent = FakeAgent('codex')
    harness = ConsoleHarness(make_settings(tmp_path), {'codex': agent})
    def build(**kwargs):
        raise RuntimeError('synthetic-spec-error')
    harness.tasks.build_spec = build
    try:
        msg = dataclasses.replace(make_message('/task codex -- X'), message_id=997712)
        await harness.dispatcher.handle_message(msg)
        rows = harness.context_store.connection.execute('SELECT status FROM tasks').fetchall()
        assert len(rows) == 1 and rows[0][0] == 'failed', rows
        await harness.dispatcher.handle_message(msg)
        assert len(agent.calls) == 0
    finally:
        await harness.aclose()
