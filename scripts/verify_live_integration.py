"""User-authorized live team + local TCP API probe; no real QQ transport."""
import asyncio,json,secrets,socket,sys,tempfile
from pathlib import Path
import httpx,uvicorn
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from helpers import ConsoleHarness
from test_api_v1 import build_api_app
from app.api.v1 import ApiIdentity
from app.config import load_settings
from app.agents.registry import AgentRegistry

async def main():
    registry=AgentRegistry.from_settings(load_settings())
    rows={}
    with tempfile.TemporaryDirectory(prefix='qq-live-integration-') as temp:
        base=Path(temp).resolve();root=base/'workspace';root.mkdir()
        nonce='TEAM_'+secrets.token_hex(8);(root/'token.txt').write_text(nonce)
        agents={name:registry.get(name) for name in ['deepseek','zcode']}
        for a in agents.values():
            if hasattr(a,'workdir'):a.workdir=str(base)
            a.runtime_options={'native_tools':False}
        settings=load_settings().model_copy(update={
            'qq_allowed_user_ids':[100],'qq_allowed_group_ids':[200],
            'database_path':str(base/'test.db'),'thinking_archive_dir':'',
            'request_timeout_seconds':90.0,'rate_limit_per_minute':100,
            'team_step_timeout_seconds':90.0,'team_workflow_timeout_seconds':240.0,
            'console_workspaces':{'roots':[{'path':str(root),'modes':['read','edit','exec'],
                'admin_modes':['read','edit','exec']}],'backup_dir':str(base/'backups')},
            'console_commands':{'broker':{'max_task_seconds':180.0},
                'exec':{'require_sandbox':True,'allow_network':False}},
        })
        h=ConsoleHarness(settings,agents)
        try:
            team=h.create_team('真实模型跨后端读写验证')
            h.add_step(team,'read','deepseek','用 read 读取 token.txt，final 仅返回文件的完整内容。',mode='read',workspace=str(root))
            h.add_step(team,'write','zcode','从直接上游成功答复中提取 TEAM_ 开头的字符串，用 propose_changes 创建 result.txt，完整内容恰好是这个字符串；完成后 final。',mode='edit',workspace=str(root),after=('read',),effort='low',effort_set=True)
            await h.command(f'/team run {team}')
            status=await h.wait_team(team,timeout=220)
            rows['team']={'status':status,'actual_file_matches':(root/'result.txt').is_file() and (root/'result.txt').read_text()==nonce,
                'steps':[{ 'id':step,'status':h.workflow_store.get_step_run(team,1,step).status,
                    'error':h.workflow_store.get_step_run(team,1,step).error} for step in ['read','write']]}
            print(json.dumps({'team':rows['team']},ensure_ascii=False),flush=True)
            token=secrets.token_urlsafe(24)
            app=build_api_app(h,tokens={token:ApiIdentity(name='live-test',user_id=100,group_ids=(200,),scopes=frozenset({'task','team','workspace'}))})
            sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(32)
            server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,lifespan='off'))
            job=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:await asyncio.sleep(.02)
                async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}',trust_env=False,timeout=10) as c:
                    denied=await c.get('/api/v1/agents')
                    headers={'Authorization':'Bearer '+token,'Idempotency-Key':secrets.token_hex(8)}
                    payload={'agent':'deepseek','mode':'text','prompt':'只返回 LIVE_HTTP_OK'}
                    first=await c.post('/api/v1/tasks',headers=headers,json=payload)
                    repeat=await c.post('/api/v1/tasks',headers=headers,json=payload)
                    taskid=first.json().get('task_id')
                    for _ in range(100):
                        fetched=await c.get(f'/api/v1/tasks/{taskid}/result',headers=headers)
                        data=fetched.json()
                        if data.get('task',{}).get('status') in ['succeeded','failed','cancelled','partial_failed']:break
                        await asyncio.sleep(.5)
                    record=h.task_store.get_task(taskid)
                    rows['http']={'real_tcp':True,'unauth_status':denied.status_code,'submit_status':first.status_code,
                        'same_task_on_repeat':repeat.json().get('task_id')==taskid,'task_status':record['status'],
                        'answer_contains_marker':'LIVE_HTTP_OK' in str(data)}
            finally:
                server.should_exit=True;await job;sock.close()
            print(json.dumps({'http':rows['http']},ensure_ascii=False),flush=True)
        finally:
            await h.aclose();await registry.aclose()
    (ROOT/'outputs/验收复现/真实team与HTTP验证.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':asyncio.run(main())
