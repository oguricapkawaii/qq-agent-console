import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from app.tasks.store import TaskStore


def test_sqlite_idempotency_is_atomic_across_processes(tmp_path):
    db=tmp_path/'processes.db'
    conn=sqlite3.connect(db)
    TaskStore(conn).migrate()
    conn.close()
    worker="""import json,sqlite3,sys,time
from app.tasks.store import TaskStore
db,start=sys.argv[1],float(sys.argv[2])
conn=sqlite3.connect(db,timeout=10)
while time.time()<start: time.sleep(0.01)
record,created=TaskStore(conn).register_idempotent_task(entry='qq',group_id=200,user_id=100,agent='codex',prompt='X',idempotency_key='same-process-test-key')
print(json.dumps({'task_id':record['task_id'],'created':created}))
conn.close()
"""
    cwd=Path(__file__).resolve().parents[1]
    start=str(time.time()+1.5)
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(cwd)}
    children=[subprocess.Popen([sys.executable,'-c',worker,str(db),start],cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
    results=[]
    for process in children:
        stdout,stderr=process.communicate(timeout=15)
        assert process.returncode==0,stderr
        results.append(json.loads(stdout))
    assert sum(r['created'] for r in results)==1,results
    assert len({r['task_id'] for r in results})==1,results
    conn=sqlite3.connect(db)
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
    conn.close()
