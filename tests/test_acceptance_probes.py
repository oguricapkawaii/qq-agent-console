"""独立验收复现（F01–F16）：已按项目惯例纳入 tests/。

来源：`outputs/验收复现/test_independent_acceptance.py`（Codex 独立验收脚本）。
这里保留**完全相同的需求断言与场景**，只调整导入与少量注释，便于随主测试集一起回归。
每条用例对应 Codex 验收报告中的一项缺陷编号（见函数上方注释）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tests"))
from helpers import requires_platform_sandbox, ConsoleHarness, ScriptedAgent, FakeAgent, workspace_settings  # noqa: E402
from test_api_v1 import build_api_app, auth, TOKEN  # noqa: E402
from app.agents.codex import BROKER_DISABLE_CANDIDATES, CodexAdapter, CodexCapabilities, CodexEvents  # noqa: E402
from app.api.v1 import ApiIdentity  # noqa: E402
from app.backends.protocol import TaskRequest  # noqa: E402
from app.commands.options import PublicOptions  # noqa: E402
from app.models import InvocationOptions  # noqa: E402
from app.security.exec_policy import ExecPolicy, run_command  # noqa: E402
from app.workspaces.broker import BrokerBudget, FileBroker  # noqa: E402
from app.workspaces.changes import ChangeSet, file_hash  # noqa: E402


def fixture_harness(tmp_path, agent=None, **overrides):
    root = tmp_path / 'workspace'
    root.mkdir()
    h = ConsoleHarness(workspace_settings(tmp_path, root=root, **overrides),
                       {'deepseek': agent or FakeAgent('deepseek', reply='{"final":"done"}')})
    return root, h


def broker(h, root, mode='read', budget=None):
    return FileBroker(acl=h.acl, root=root, group_id=200, user_id=100,
                      task_id='probe', mode=mode, budget=budget, exec_policy=ExecPolicy())


# F01 exec 必须有可验证隔离（写边界）
@requires_platform_sandbox()
async def test_exec_cannot_write_outside_workspace(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside_sentinel.txt'
    outside.write_text('unchanged')
    # All files are our temporary fixtures. Never use real credentials/user files.
    argv = [sys.executable, '-c', f"from pathlib import Path; Path({str(outside)!r}).write_text('ESCAPED')"]
    result = await run_command(argv, workspace=root, cwd=root, policy=ExecPolicy())
    assert outside.read_text() == 'unchanged', f'escaped workspace: exit={result.exit_code}'


# F02 broker/read 不得启用原生可写工具
async def test_codex_broker_read_does_not_enable_native_writes(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    adapter = CodexAdapter(False, binary='not-a-real-program', cache_path=tmp_path/'missing')
    adapter.binary = '/unused/fixture/codex'
    # V03：能力探测是安全闸门的一部分，这里先给出"已探测成功"的确定性前提，
    # 才能真正走到 argv 构造与进程启动（"探测失败即拒绝启动"由
    # tests/test_codex_selection.py::test_probe_failure_refuses_to_start 覆盖）。
    adapter._capabilities = CodexCapabilities(
        binary=adapter.binary, version='codex-cli fixture',
        features=frozenset(BROKER_DISABLE_CANDIDATES), probed=True,
    )
    captured = []
    async def capture(*args, **kwargs):
        captured.extend(args)
        raise OSError('fixture stops before spawning')
    with patch('app.agents.codex.asyncio.create_subprocess_exec', capture):
        with pytest.raises(OSError):
            await adapter._run([], InvocationOptions(model='fixture', directory=str(root),
                              workspace_tools=False, mode='read'), CodexEvents())
    assert not any('"."="write"' in str(arg) for arg in captured), 'read broker received native writable profile'
    # 只读限根 profile 必须真的下发，且不得与 --sandbox 同传（同传会顶掉 profile）
    assert any(str(arg).startswith('default_permissions="') for arg in captured)
    assert any(str(arg).startswith('permissions.') for arg in captured)
    assert '--sandbox' not in [str(arg) for arg in captured]
    assert 'shell_tool' in captured and 'unified_exec' in captured


# F03 search 不得通过符号链接别名泄漏敏感文件
async def test_search_does_not_follow_alias_to_sensitive_file(tmp_path):
    root, h = fixture_harness(tmp_path)
    try:
        (root/'.env').write_text('FAKE_SECRET_NEEDLE=fixture-only')
        (root/'innocent.txt').symlink_to(root/'.env')
        result = await broker(h, root).execute({'action':'search','query':'FAKE_SECRET_NEEDLE'})
        assert not result.payload.get('hits'), result.payload
    finally:
        await h.aclose()


# F03 search 必须复查受保护子目录
async def test_search_rechecks_nested_protected_paths(tmp_path):
    root = tmp_path/'workspace'
    root.mkdir()
    protected = root/'private'
    protected.mkdir()
    (protected/'memo.txt').write_text('PROTECTED_FIXTURE_NEEDLE')
    settings = workspace_settings(tmp_path, root=root, console_workspaces={
        'roots':[{'path':str(root),'modes':['read'],'admin_modes':['read']}],
        'protected_paths':[str(protected)]})
    h = ConsoleHarness(settings, {'deepseek':FakeAgent('deepseek')})
    try:
        b = broker(h, root)
        direct = await b.execute({'action':'read','path':'private/memo.txt'})
        assert not direct.ok
        result = await b.execute({'action':'search','query':'PROTECTED_FIXTURE_NEEDLE'})
        assert not result.payload.get('hits'), result.payload
    finally:
        await h.aclose()


# F12 search 必须计入累计读取预算
async def test_search_has_read_byte_budget(tmp_path):
    root, h = fixture_harness(tmp_path)
    try:
        (root/'large.txt').write_text('SEARCH_NEEDLE\n'*1000)
        b = broker(h, root, budget=BrokerBudget(max_file_bytes=16,max_total_read_bytes=16))
        result = await b.execute({'action':'search','query':'SEARCH_NEEDLE'})
        assert not result.ok or b.read_bytes > 0, 'search read file outside byte accounting'
    finally:
        await h.aclose()


# F04 备份不得跟随符号链接写出授权目录
async def test_backup_cannot_follow_outside_symlink(tmp_path):
    root, h = fixture_harness(tmp_path)
    try:
        outside = tmp_path/'outside_backup_sentinel.txt'
        outside.write_text('unchanged')
        (root/'a.txt').write_text('old')
        (root/'a.txt.bak').symlink_to(outside)
        b = broker(h, root, mode='edit')
        result = await b.execute({'action':'propose_changes','changes':[{'path':'a.txt','content':'new'}]})
        assert result.ok
        b.commit()
        assert outside.read_text() == 'unchanged', 'backup write escaped via symlink'
    finally:
        await h.aclose()


# F06 规划版本必须强制复核
def test_staged_write_detects_changes_without_optional_model_hash(tmp_path):
    target = tmp_path/'a.txt'
    target.write_text('before')
    changes = ChangeSet(root=tmp_path)
    changes.add_write(target, 'planned')
    target.write_text('concurrent-user-edit')
    changes.apply()
    assert target.read_text() == 'concurrent-user-edit', 'plan-time before_hash was not enforced'


# F11 HTTP 选择目录后必须续跑原任务
async def test_http_selection_resumes_original_task(tmp_path):
    root, h = fixture_harness(tmp_path)
    (root/'target-project').mkdir()
    app = build_api_app(h)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            created = await c.post('/api/v1/tasks',headers=auth(),json={
                'agent':'deepseek','prompt':'read','find':'target','mode':'read'})
            assert created.status_code == 202
            data = created.json()
            selected = await c.post('/api/v1/workspaces/select',headers=auth(),json={
                'code':data['selection_code'],'index':1})
            assert selected.status_code == 200
            await asyncio.sleep(.02)
            if app.state.background:
                await asyncio.gather(*app.state.background)
            record = h.task_store.get_task(data['task_id'])
            assert record['status'] != 'awaiting_selection', 'HTTP selected but never resumed task'
            assert len(h.agents['deepseek'].calls) == 1
    finally:
        await h.aclose()


# F05 已取消的等锁任务不得再调用模型或写盘
async def test_cancelled_lock_waiter_never_calls_model_or_writes(tmp_path):
    script = ['{"actions":[{"action":"propose_changes","changes":[{"path":"marker.txt","content":"unexpected"}]}]}',
              '{"final":"done"}']
    root, h = fixture_harness(tmp_path, ScriptedAgent('deepseek',script))
    pending = None
    try:
        req = TaskRequest(entry='qq',group_id=200,user_id=100,agent='deepseek',prompt='edit',
                          mode='edit',workspace=root,task_id='cancel-probe')
        spec, error = h.tasks.build_spec(request=req,workspace=root,mode='edit')
        assert spec is not None, error
        async with h.coordinator.lock([root],label='fixture-holder'):
            pending = asyncio.create_task(h.tasks.run(req,spec=spec))
            for _ in range(100):
                if h.task_store.get_task('cancel-probe') is not None:
                    break
                await asyncio.sleep(.001)
            await h.tasks.cancel(task_id='cancel-probe',group_id=200,user_id=100)
            assert h.task_store.get_task('cancel-probe')['status'] == 'cancelled'
        await pending
        assert not h.agents['deepseek'].calls and not (root/'marker.txt').exists(), (
            f"cancelled waiter ran: calls={len(h.agents['deepseek'].calls)}, "
            f"marker_exists={(root/'marker.txt').exists()}, "
            f"record_status={h.task_store.get_task('cancel-probe')['status']}")
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)
        await h.aclose()


# F08 HTTP scope 不得被 team 操作绕过
async def test_workspace_scoped_token_cannot_run_team(tmp_path):
    root, h = fixture_harness(tmp_path, FakeAgent('deepseek',reply='done'))
    team = h.create_team()
    h.add_step(team,'s1','deepseek')
    app = build_api_app(h,tokens={TOKEN:ApiIdentity(name='workspace-only',user_id=100,
                             group_ids=(200,),scopes=frozenset({'workspace'}))})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            response = await c.post(f'/api/v1/teams/{team}/run',headers=auth(),json={})
            assert response.status_code == 403, response.json()
    finally:
        await h.aclose()


# F13 目录被替换后确认必须失效
async def test_confirm_rejects_replaced_directory_identity(tmp_path):
    root, h = fixture_harness(tmp_path)
    candidate = root/'target-project'
    candidate.mkdir()
    try:
        resolution = h.workspaces.resolve_selector(group_id=200,user_id=100,
                  options=PublicOptions(find='target',mode='read'),mode='read',purpose='workspace')
        candidate.rename(root/'old-project')
        candidate.mkdir()  # Same path, different directory.
        selected = h.workspaces.select(group_id=200,user_id=100,code=resolution.selection_code,index=1)
        assert not selected.ok, 'confirmation authorized a different directory at same pathname'
    finally:
        await h.aclose()


# F07 提交失败不得冒充任务成功
async def test_commit_failure_does_not_report_task_success(tmp_path):
    root = tmp_path/'workspace'
    root.mkdir()
    target = root/'a.txt'
    target.write_text('old')
    script = [json.dumps({'actions':[{'action':'propose_changes','changes':[
                  {'path':'a.txt','content':'new','expected_sha256':file_hash(target)}]}]}),
              '{"final":"changed"}']
    agent = ScriptedAgent('deepseek',script)
    original = agent.chat
    async def alter_before_final(messages, *, timeout):
        if len(agent.calls) == 1:
            target.write_text('concurrent-user-edit')
        return await original(messages,timeout=timeout)
    agent.chat = alter_before_final
    h = ConsoleHarness(workspace_settings(tmp_path,root=root),{'deepseek':agent})
    try:
        await h.command(f'/task deepseek --dir "{root}" --mode edit -- update')
        record = h.task_store.list_recent(group_id=200,user_id=100,limit=1)[0]
        assert target.read_text() == 'concurrent-user-edit'
        assert record['status'] == 'failed', 'all writes failed but task was marked succeeded'
    finally:
        await h.aclose()


# F09 旧管理员只读根不得扩大给成员
async def test_legacy_admin_read_root_does_not_grant_member_read(tmp_path):
    root, h = fixture_harness(tmp_path,console_workspaces={},file_read_root=str(tmp_path/'workspace'))
    try:
        await h.command('/access add 101')
        assert h.authz.user_allowed(101,200)
        decision = h.acl.authorize_path(user_id=101,group_id=200,path=root,mode='read')
        assert not decision.allowed, 'legacy admin-only /read silently became member read'
    finally:
        await h.aclose()


# F14 team 单步时限必须真正生效
async def test_team_step_timeout_is_actually_enforced(tmp_path):
    root, h = fixture_harness(tmp_path,FakeAgent('deepseek',reply='done',delay=.08),
                             team_step_timeout_seconds=.02)
    try:
        team = h.create_team()
        h.add_step(team,'slow','deepseek')
        await h.command(f'/team run {team}')
        state = await h.wait_team(team)
        assert state == 'failed', f'team ignored 20ms step deadline: {state}'
    finally:
        await h.aclose()


# F10 输入漂移后不得复用只读结论
async def test_team_read_result_is_not_reused_after_input_drift(tmp_path):
    root = tmp_path/'workspace'
    root.mkdir()
    source = root/'a.txt'
    source.write_text('v1')
    reader = ScriptedAgent('deepseek',[
        '{"actions":[{"action":"read","path":"a.txt"}]}',
        '{"final":"observed v1"}'])
    failing = FakeAgent('zcode',error='network')
    h = ConsoleHarness(workspace_settings(tmp_path,root=root),{'deepseek':reader,'zcode':failing})
    try:
        team=h.create_team()
        h.add_step(team,'read','deepseek',mode='read',workspace=str(root))
        h.add_step(team,'later','zcode',after=('read',))
        await h.command(f'/team run {team}')
        assert await h.wait_team(team) == 'failed'
        source.write_text('external-v2')
        response=await h.command(f'/team retry {team}')
        assert '漂移' in response, 'stale read answer reused after input file changed'
        assert h.workflow_store.get_workflow(team).round == 1
    finally:
        await h.aclose()


# F15 未实现跨进程锁必须限制单 worker
def test_multiworker_cannot_be_enabled_by_boolean_without_lock_implementation(tmp_path):
    from app.workspaces.configurator import build_workspace_config
    from helpers import make_settings
    settings=make_settings(tmp_path,console_workspaces={'max_workers':2,'cross_process_lock':True})
    with pytest.raises(ValueError):
        build_workspace_config(settings)
