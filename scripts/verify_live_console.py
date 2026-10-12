"""Explicit live acceptance; temp workspace/DB, real adapters, recording transport.
Run only on user authorization: creates paid model calls, never sends QQ messages.
"""
import argparse
import asyncio
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from helpers import ConsoleHarness
from app.config import load_settings
from app.agents.registry import AgentRegistry
from app.backends.protocol import TaskRequest

async def main(phase, names, timeout=60.0, effort=None):
    loaded = load_settings()
    registry = AgentRegistry.from_settings(loaded)
    rows = []
    suffix = f'_{effort}' if effort else ''
    destination = ROOT / 'outputs/验收复现' / f'真实后端验证_{phase}{suffix}.json'
    with tempfile.TemporaryDirectory(prefix='qq-live-acceptance-') as directory:
        base = Path(directory).resolve()
        workspace = base / 'workspace'; workspace.mkdir()
        agents = {name: registry.get(name) for name in names}
        for agent in agents.values():
            if hasattr(agent, 'workdir'):
                agent.workdir = str(base)
            agent.runtime_options = {'native_tools': False}
        settings = loaded.model_copy(update={
            'qq_allowed_user_ids':[100], 'qq_allowed_group_ids':[200],
            'database_path':str(base/'test.db'), 'thinking_archive_dir':'',
            'request_timeout_seconds':timeout, 'rate_limit_per_minute':100,
            'team_step_timeout_seconds':timeout, 'team_workflow_timeout_seconds':240.0,
            'console_workspaces':{'roots':[{'path':str(workspace),
                'modes':['read','edit','exec'],'admin_modes':['read','edit','exec']}],
                'backup_dir':str(base/'backups')},
            'console_commands':{'broker':{'max_task_seconds':120.0},
                                'exec':{'require_sandbox':True,'allow_network':False}},
        })
        harness = ConsoleHarness(settings, agents)
        try:
            for name, agent in agents.items():
                nonce = uuid.uuid4().hex[:12]
                source = workspace/f'{name}-source.txt'
                edit = workspace/f'{name}-edit.txt'
                executed = workspace/f'{name}-exec.txt'
                token = 'READ_'+nonce
                source.write_text(token)
                edit.write_text('before')
                if phase == 'text':
                    mode='text'; prompt='只返回 LIVE_TEXT_OK，不要使用工具。'
                elif phase == 'read':
                    mode='read'; prompt=f'用 read 动作读取 {source.name}。然后 final 原样返回文件内容，不做其他操作。'
                elif phase == 'edit':
                    mode='edit'; prompt=f'把 {edit.name} 的完整内容改为 LIVE_EDIT_{nonce}。必须用 propose_changes 动作，完成后 final。'
                else:
                    mode='exec'; prompt=(f'用 run_command 执行 argv 数组 ["python3", "-c", '
                        f'"from pathlib import Path; Path({executed.name!r}).write_text(\"LIVE_EXEC_{nonce}\")"]。'
                        '完成后 final，不请求其他动作。')
                request=TaskRequest(entry='qq',group_id=200,user_id=100,agent=name,
                    prompt=prompt,mode=mode,workspace=workspace if mode!='text' else None,
                    task_id='live-'+uuid.uuid4().hex[:12],use_chat_context=False,
                    effort=effort,effort_set=effort is not None)
                spec,error=harness.tasks.build_spec(request=request,
                    workspace=request.workspace,mode=mode,deadline_seconds=120)
                started=time.monotonic()
                if spec is None:
                    row={'agent':name,'mode':mode,'ok':False,'error':error}
                else:
                    result=await harness.tasks.run(request,spec=spec)
                    if phase=='text': observed='LIVE_TEXT_OK' in result.content
                    elif phase=='read': observed=token in result.content
                    elif phase=='edit': observed=edit.read_text()==f'LIVE_EDIT_{nonce}'
                    else: observed=executed.is_file() and executed.read_text()==f'LIVE_EXEC_{nonce}'
                    row={'agent':name,'mode':mode,'model':result.model,'effort':result.effort,'status':result.status,
                        'ok':result.ok,'error':result.error,'detail':result.detail,
                        'observed':observed,'answer':result.content[:1000],
                        'native_tools':False}
                row['seconds']=round(time.monotonic()-started,2)
                rows.append(row)
                destination.write_text(json.dumps({'phase':phase,'real_models':True,
                    'qq_messages_sent':False,'temporary_workspace':True,'results':rows},
                    ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(row,ensure_ascii=False),flush=True)
        finally:
            await harness.aclose()
            await registry.aclose()
    return 0 if all(row.get('ok') and row.get('observed') for row in rows) else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',choices=['text','read','edit','exec'],required=True)
    parser.add_argument('--agents',nargs='+',default=['deepseek','zcode','codex'])
    parser.add_argument('--timeout',type=float,default=60.0)
    parser.add_argument('--effort')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.phase,args.agents,args.timeout,args.effort)))
