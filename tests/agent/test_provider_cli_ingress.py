"""Real CLI and canonical Bot Chat subprocess: no provider quota is consumed."""
import os
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
import yaml


@pytest.mark.parametrize('bot_chat',[False,True],ids=['direct-cli','canonical-bot-chat'])
def test_native_cli_ingress_hold_is_resumable_without_inference(tmp_path,bot_chat):
    root=Path(__file__).resolve().parents[2]
    home=tmp_path/'estate'
    home.mkdir()
    db=home/'holds.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT,since REAL,incident TEXT)')
        conn.execute('INSERT INTO holds VALUES(?,?,?)',('anthropic',1,'fixture'))
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length',0)))
            requests.append((self.path,body))
            self.send_response(500); self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    cfg=dict(model=dict(provider='anthropic',default='claude-sonnet-4-5',base_url=url),
        provider_control=dict(database=str(db),providers=['anthropic','openai-codex']),
        memory=dict(memory_enabled=False,user_profile_enabled=False),
        compression=dict(enabled=False),curator=dict(enabled=False),
        agent=dict(max_iterations=1),display=dict(interface='cli'),
        auxiliary={name:dict(provider='anthropic',model='claude-sonnet-4-5',base_url=url)
                   for name in ('compression','vision','title','session_title')})
    (home/'config.yaml').write_text(yaml.safe_dump(cfg))
    env=dict(os.environ,HOME=str(tmp_path),HERMES_HOME=str(home),HERMES_PROFILE='default',
        ANTHROPIC_API_KEY='sk-ant-api03-synthetic-fixture',ANTHROPIC_BASE_URL=url,
        OPENAI_BASE_URL=url,NO_PROXY='127.0.0.1,localhost')
    for key in ('ANTHROPIC_TOKEN','CLAUDE_CODE_OAUTH_TOKEN','OPENAI_API_KEY','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY'):
        env.pop(key,None)
    query='Preserve this synthetic pending message without inference.'
    args=[sys.executable,'-m','hermes_cli.main','chat','-Q','--query',query]
    if bot_chat:
        from tools.bot_relay import BOT_CHAT_TURN_ARGS
        args=[sys.executable,'-m','hermes_cli.main',*BOT_CHAT_TURN_ARGS,'--query',query]
    try:
        result=subprocess.run(args,cwd=root,env=env,text=True,capture_output=True,timeout=40)
        (tmp_path/'cli-receipt.txt').write_text(f'exit={result.returncode}\n'+result.stdout+result.stderr)
        assert 'Provider held; explicit operator resume required' in result.stdout+result.stderr, (result.returncode,result.stdout,result.stderr)
        assert 'Traceback' not in result.stderr
        # Local endpoints trigger native Ollama model metadata probing during
        # construction; /api/show is not inference. Reject every other POST.
        assert all(path=='/api/show' and 'messages' not in json.loads(body) for path,body in requests), requests
        with sqlite3.connect(home/'state.db') as conn:
            rows=conn.execute('SELECT role,content FROM messages ORDER BY timestamp').fetchall()
            assert any(role=='user' and content==query for role,content in rows), rows
            assert not any(role=='assistant' for role,content in rows), rows
        assert db.exists()
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT provider FROM holds').fetchall()==[('anthropic',)]
    finally:
        server.shutdown();server.server_close()
