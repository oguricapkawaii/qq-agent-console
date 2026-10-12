import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.agents.deepseek_harness import DeepSeekHarnessAdapter, task_patch
from app.agents.harness_process import run_process
from app.agents.zcode import (FILE_TOOLS, PROTECTED_DIR, ZCodeAdapter, ZCodeProtocol, ProtocolError,
                              make_approver, prepare_home, tool_log)
from app.models import ChatMessage, InvocationOptions


def test_deepseek_patch_uses_native_effort_and_blocks_tools():
    patch = task_patch(InvocationOptions('deepseek-flash', 'max'), 'https://api.deepseek.com/anthropic')
    rows = {row['id']: row for row in patch}
    assert rows['agent-default-model']['config'] == {'provider': 'deepseek-official', 'model': 'deepseek-flash', 'reasoningEffort': 'max'}
    assert rows['llm-deepseek']['config']['baseURL'].endswith('/anthropic')
    for tool in ('tool-bash', 'tool-fs', 'tool-subagent', 'tool-web', 'session-telemetry-otel'):
        assert rows[tool]['disabled'] is True
    assert 'reasoningEffort' not in task_patch(InvocationOptions('deepseek-flash'), 'url')[0]['config']


async def test_deepseek_runs_cli_not_api_and_cleans_home(monkeypatch, tmp_path):
    captured = {}
    async def run(argv, *, env, cwd, stdin):
        captured.update(argv=argv, env=env, cwd=cwd, stdin=stdin)
        assert Path(env['DSH_HOME']).exists()
        assert env['DEEPSEEK_API_KEY'] == 'private-key'
        assert env['DSH_PERMISSION_MODE'] == 'read-only'
        assert not any('private-key' in arg or '$(touch' in arg for arg in argv)
        return 0, '\n'.join([
            json.dumps({'type': 'session', 'sessionId': 'session-1', 'cwd': cwd}),
            json.dumps({'type': 'status', 'phase': 'step_start', 'turn': 1, 'step': 1}),
            json.dumps({'type': 'thinking', 'text': '先确认问题范围。'}),
            json.dumps({'type': 'text', 'text': 'real process reply'}),
            json.dumps({'type': 'final', 'text': 'real process reply'}),
        ])
    monkeypatch.setattr('app.agents.deepseek_harness.run_process', run)
    agent = DeepSeekHarnessAdapter(api_key='private-key', binary=sys.executable, workdir=str(tmp_path))
    reply = await agent.chat_with_options([ChatMessage('user', '$(touch bad); --patch evil')], timeout=1, options=InvocationOptions('deepseek-flash', 'low'))
    assert reply.content == 'real process reply'
    assert reply.thinking == '先确认问题范围。'
    assert '$(touch bad)' in captured['stdin']
    assert captured['argv'][-1] == '-'
    # --patch 是启动器选项，必须排在应用选项 --json 之前，否则 CLI 直接报 unknown option。
    assert captured['argv'].index('--patch') < captured['argv'].index('--json')
    assert not Path(captured['cwd']).exists()
    with pytest.raises(ValueError):
        await agent.chat_with_options([], timeout=1, options=InvocationOptions('deepseek-flash', 'medium'))


def _patch_runner(monkeypatch, code, text):
    async def run(argv, *, env, cwd, stdin):
        return code, text
    monkeypatch.setattr('app.agents.deepseek_harness.run_process', run)


async def _deepseek_reply(monkeypatch, tmp_path, code, text, **kwargs):
    _patch_runner(monkeypatch, code, text)
    agent = DeepSeekHarnessAdapter(api_key='k', binary=sys.executable, workdir=str(tmp_path), **kwargs)
    return await agent.chat_with_options([ChatMessage('user', 'q')], timeout=1,
                                         options=InvocationOptions('deepseek-flash', 'low'))


async def test_deepseek_json_events_split_answer_and_thinking(monkeypatch, tmp_path):
    stream = '\n'.join([
        json.dumps({'type': 'thinking', 'text': '第一段思考。'}),
        json.dumps({'type': 'thinking', 'text': '第二段思考。'}),
        json.dumps({'type': 'text', 'text': '半截正文不应被当作答复'}),
        json.dumps({'type': 'final', 'text': '最终答复。'}),
    ])
    reply = await _deepseek_reply(monkeypatch, tmp_path, 0, stream)
    assert reply.error is None
    assert reply.content == '最终答复。'
    assert reply.thinking == '第一段思考。\n第二段思考。'


@pytest.mark.parametrize('stream', [
    '',
    '\n\n',
    'not json at all',
    '{"type":"final"}',
    '{"type":"final","text":""}',
    '[1,2,3]',
    '{"type":123,"text":"忽略"}',
    json.dumps({'type': 'thinking', 'text': '只有思考没有答复'}),
])
async def test_deepseek_malformed_or_empty_stream_reports_harness_failed(monkeypatch, tmp_path, stream):
    reply = await _deepseek_reply(monkeypatch, tmp_path, 0, stream)
    assert reply.error == 'harness_failed'
    assert reply.content == ''
    # 原始事件流不得被当作答复或思考过程回显。
    assert reply.thinking == ''
    if stream:
        assert stream not in (reply.content, reply.thinking)


async def test_deepseek_error_exit_keeps_message_out_of_reply(monkeypatch, tmp_path):
    secret_line = json.dumps({'type': 'error', 'message': 'api key sk-do-not-leak rejected'})
    reply = await _deepseek_reply(monkeypatch, tmp_path, 1, secret_line)
    assert reply.error == 'harness_failed'
    assert 'sk-do-not-leak' not in reply.content
    assert 'sk-do-not-leak' not in (reply.thinking or '')


def test_zcode_home_has_complete_private_schema(tmp_path):
    provider = prepare_home(tmp_path, 'secret', 'https://example.test/v1', ['GLM-5.3'])
    assert provider.stat().st_mode & 0o777 == 0o600
    config = json.loads(provider.read_text())['config']
    assert config['modelConfigRules'] == {'providerModelRules': [], 'manualProviderModelRules': []}
    assert config['providerConfigRules']['providerRules'][0]['config']['access']['apiKey'] == 'secret'
    user = json.loads((tmp_path / '.zcode/cli/config.json').read_text())
    assert user['plugins']['enabled'] is False
    assert user['features']['mcp'] is False


class Writer:
    def __init__(self):
        self.values = []
    def write(self, data):
        self.values.append(json.loads(data))
    async def drain(self):
        pass


class Process:
    returncode = 0
    def __init__(self, rows):
        self.stdin = Writer()
        self.stdout = asyncio.StreamReader()
        for row in rows:
            self.stdout.feed_data((json.dumps(row) + '\n').encode())
        self.stdout.feed_eof()


async def test_zcode_protocol_handshake_and_rejects_approval():
    proc = Process([
        {'id':'server-1', 'method':'session/requestRuntimePreferences'},
        {'id':1, 'result': {'ok':True}},
        {'id':'server-2', 'method':'interaction/requestPermission'},
    ])
    rpc = ZCodeProtocol(proc)
    assert await rpc.request('session/create', {}) == {'ok':True}
    assert 'jsonrpc' not in proc.stdin.values[0]
    assert proc.stdin.values[1]['result']['askUserQuestionAutoResolutionEnabled'] is False
    with pytest.raises(ProtocolError) as exc:
        await rpc.request('session/send', {})
    assert exc.value.kind == 'harness_approval_required'
    assert 'error' in proc.stdin.values[-1]


@pytest.mark.parametrize('raw', [None, {'id':1,'error':{'code':-1}}, {'id':1}, []])
async def test_zcode_protocol_failures(raw):
    proc = Process([] if raw is None else [raw])
    with pytest.raises(ProtocolError):
        await ZCodeProtocol(proc).request('session/create', {})


async def test_zcode_protocol_session_output_limit(monkeypatch):
    """轮询快照按会话累计上限防护：超限报 output_limit，而不是单次读取上限。"""
    monkeypatch.setattr('app.agents.zcode.MAX_SESSION_BYTES', 10)
    proc = Process([{'id':1,'result':{'ok':True}}])
    with pytest.raises(ProtocolError) as exc:
        await ZCodeProtocol(proc).request('session/create', {})
    assert exc.value.kind == 'output_limit'


@pytest.mark.parametrize('guest', [False, True])
async def test_zcode_selected_model_effort_and_final_text(monkeypatch, tmp_path, guest):
    entry = tmp_path/'entry'; entry.touch()
    builtin = tmp_path/'builtin'; builtin.touch()
    catalog = tmp_path/'catalog.json'
    catalog.write_text(json.dumps({'glm-5.3':{'model_id':'GLM-5.3','efforts':['low','high'], 'default_effort':'high'}}))
    selected = {'providerId':'bigmodel-standard-api','modelId':'GLM-5.3'}
    proc = Process([
        {'id':1,'result':{'session':{'sessionId':'s'},'settings':{'model':{'current':selected}}}},
        {'id':2,'result':{}},
        {'id':3,'result':{'snapshot':{'settings':{'thoughtLevel':{'current':'low'}}}}},
        {'id':4,'result':{'accepted':True}},
        {'id':5,'result':{'projection':{'status':'idle'},'messages':[{'info':{'role':'assistant'},'parts':[
            {'type':'reasoning','text':'model thinking'},
            {'type':'text','text':'harness reply'}]}]}},
    ])
    captured = {}
    async def spawn(*argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        assert Path(kwargs['env']['ZCODE_PERSONAL_PROVIDER_CONFIG_FILE']).exists()
        return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    agent = ZCodeAdapter(api_key='private-key',base_url='https://example.test/v1',enabled=True,node=sys.executable,entry=str(entry),builtin=str(builtin),catalog=str(catalog),workspace_root=str(tmp_path) if guest else '')
    reply = await agent.chat_with_options([ChatMessage('user','hello')],timeout=1,options=InvocationOptions('glm-5.3','low',workspace_tools=not guest))
    assert reply.content == 'harness reply'
    assert reply.thinking == 'model thinking'
    requests = proc.stdin.values
    assert requests[0]['params']['model']['options']['reasoningLevel'] == 'low'
    assert requests[0]['params']['toolAllowlist'] == ['__qq_no_tools__']
    assert requests[0]['params']['workspace']['workspacePath'] != str(tmp_path)
    assert requests[1]['method'] == 'session/setMode'
    assert requests[2]['params']['thoughtLevel'] == 'low'
    assert 'private-key' not in str(captured['argv'])
    assert not Path(captured['kwargs']['cwd']).exists()


async def test_zcode_workspace_mode_enables_file_tools(monkeypatch, tmp_path):
    """配置工作目录后：只开文件工具、不设 plan 模式、提示词带目录约束、返回工具日志。"""
    root = tmp_path / 'ws'
    root.mkdir()
    entry = tmp_path/'entry'; entry.touch()
    builtin = tmp_path/'builtin'; builtin.touch()
    catalog = tmp_path/'catalog.json'
    catalog.write_text(json.dumps({'glm-5.3':{'model_id':'GLM-5.3','efforts':['low','high'], 'default_effort':'high'}}))
    selected = {'providerId':'bigmodel-standard-api','modelId':'GLM-5.3'}
    edited = root / 'a.txt'
    proc = Process([
        {'id':1,'result':{'session':{'sessionId':'s'},'settings':{'model':{'current':selected}}}},
        {'id':2,'result':{'snapshot':{'settings':{'thoughtLevel':{'current':'low'}}}}},
        {'id':3,'result':{'accepted':True}},
        {'id':4,'result':{'projection':{'status':'idle'},'messages':[{'info':{'role':'assistant'},'parts':[
            {'type':'text','text':'file done'},
            {'type':'tool','tool':'Edit','state':{'status':'completed','input':{'file_path':str(edited)}}}]}]}},
    ])
    async def spawn(*argv, **kwargs):
        return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    agent = ZCodeAdapter(api_key='private-key',base_url='https://example.test/v1',enabled=True,
        node=sys.executable,entry=str(entry),builtin=str(builtin),catalog=str(catalog),
        workspace_root=str(root))
    reply = await agent.chat_with_options([ChatMessage('user','改一下a.txt')],timeout=1,options=InvocationOptions('glm-5.3','low'))
    assert reply.content == 'file done'
    assert reply.tool_log == [f'Edit {edited} [completed]']
    requests = proc.stdin.values
    assert requests[0]['params']['toolAllowlist'] == list(FILE_TOOLS)
    assert requests[0]['params']['workspace']['workspacePath'] == str(root)
    assert 'session/setMode' not in [r['method'] for r in requests]
    sent = next(r for r in requests if r['method'] == 'session/send')
    assert str(root) in sent['params']['content']


async def test_zcode_approver_only_allows_paths_inside_workspace(tmp_path):
    root = tmp_path / 'ws'
    root.mkdir()
    approve = make_approver(root)
    assert approve({'toolName': 'Write', 'input': {'file_path': str(root / 'a.txt'), 'content': 'x'}}) == (True, '')
    outside, why = approve({'toolName': 'Edit', 'input': {'file_path': str(root / '..' / 'escape.txt')}})
    assert not outside and '工作目录' in why
    protected, why = approve({'toolName': 'Write', 'input': {'file_path': str(PROTECTED_DIR / '.env')}})
    assert not protected and '受保护' in why
    assert not approve({'toolName': 'Bash', 'input': {'command': 'ls'}})[0]
    assert not approve({'toolName': 'Write', 'input': {'content': '没有路径'}})[0]
    # 读类工具的 path 字段同样受边界约束
    assert approve({'toolName': 'Grep', 'input': {'pattern': 'x', 'path': str(root)}})[0]
    assert not approve({'toolName': 'Glob', 'input': {'pattern': '**/*', 'path': '/etc'}})[0]


async def test_zcode_protocol_auto_answers_permissions_without_raising():
    """文件模式：权限请求按审批器结果回应，任务不中断。"""
    def approver(params):
        return params['toolName'] == 'Write', '目标不在工作目录内'
    proc = Process([
        {'id':'server-1','method':'interaction/requestPermission',
         'params':{'toolName':'Write','input':{'file_path':'/tmp/ok.txt'}}},
        {'id':1,'result':{'ok':True}},
        {'id':'server-2','method':'interaction/requestPermission',
         'params':{'toolName':'Edit','input':{'file_path':'/etc/passwd'}}},
        {'id':2,'result':{'ok':True}},
    ])
    rpc = ZCodeProtocol(proc, approver=approver)
    assert await rpc.request('session/create', {}) == {'ok':True}
    assert await rpc.request('session/read', {}) == {'ok':True}
    decisions = [v['result'] for v in proc.stdin.values
                 if isinstance(v.get('result'), dict) and 'decision' in v['result']]
    assert decisions[0] == {'decision': 'allow'}
    assert decisions[1]['decision'] == 'deny'
    assert '工作目录' in decisions[1]['reason']
    assert rpc.permission_log == ['Write allow', 'Edit deny:目标不在工作目录内']


def test_zcode_tool_log_marks_out_of_scope_operations(tmp_path):
    root = tmp_path
    snapshot = {'messages': [{'parts': [
        {'type':'tool','tool':'Edit','state':{'status':'completed','input':{'file_path':str(root/'a.md')}}},
        {'type':'tool','tool':'Read','state':{'status':'completed','input':{'file_path':'/etc/hosts'}}},
        {'type':'tool','tool':'Read','state':{'status':'completed','input':{'file_path':str(PROTECTED_DIR/'.env')}}},
        {'type':'text','text':'answer'},
    ]}]}
    entries = tool_log(snapshot, root)
    assert entries[0] == f"Edit {root/'a.md'} [completed]"
    assert '⚠️工作目录外' in entries[1]
    assert '⚠️受保护目录' in entries[2]


async def test_process_boundary_stdin_and_output_limit(tmp_path, monkeypatch):
    code, text = await run_process([sys.executable, '-c', 'import sys; print(sys.stdin.read())'],env={'PATH':os.environ['PATH']},cwd=str(tmp_path),stdin='$(not-a-command)')
    assert code == 0 and text.strip() == '$(not-a-command)'
    monkeypatch.setattr('app.agents.harness_process.MAX_OUTPUT_BYTES', 10)
    with pytest.raises(ValueError, match='output_limit'):
        await run_process([sys.executable, '-c', 'print("x"*100)'],env={},cwd=str(tmp_path),stdin='')
