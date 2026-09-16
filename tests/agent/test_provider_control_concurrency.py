"""Two shared-account attempts stop; an in-flight other provider survives."""
import json
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from openai import OpenAI
from agent import auxiliary_client as aux, provider_control as pc
from agent.process_bootstrap import build_keepalive_http_client


def test_concurrent_hold_scope_and_inflight_sibling(tmp_path, monkeypatch):
    db = tmp_path/'holds.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT,since REAL,incident TEXT)')
    monkeypatch.setattr(pc, 'current_policy', lambda: pc.Policy(db, ('openai-codex',)))
    entered = {name: threading.Event() for name in ('A','B','Q')}
    closed = {name: threading.Event() for name in ('A','B')}
    release = threading.Event()
    paths, outcomes = [], {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args): pass
        def do_POST(self):
            name = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['model']
            paths.append(name)
            entered[name].set()
            if name != 'Q':
                self.connection.settimeout(10)
                try:
                    if self.connection.recv(1) == b'': closed[name].set()
                except (BrokenPipeError, ConnectionResetError): closed[name].set()
                except OSError: pass
                return
            release.wait(10)
            body = json.dumps(dict(id='local',object='chat.completion',created=0,model=name,
                choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content='fixture-Q'))])).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    cached = OpenAI(api_key='local-fixture',base_url=url,max_retries=0,
                    http_client=build_keepalive_http_client(url),timeout=10)
    def owner(name):
        try:
            result = aux._relay_sync_completion(cached,dict(model=name,messages=[dict(role='user',content='local')]),
                provider='xai' if name=='Q' else 'openai-codex')
            outcomes[name] = result.choices[0].message.content
        except BaseException as exc:
            outcomes[name] = type(exc).__name__
    threads = {name:threading.Thread(target=owner,args=(name,),daemon=True) for name in entered}
    try:
        for thread in threads.values(): thread.start()
        for event in entered.values(): assert event.wait(15), outcomes
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)',('openai-codex',1,'local'))
        for name in ('A','B'):
            assert closed[name].wait(2), name
            threads[name].join(2)
            assert outcomes.get(name)=='AuxiliaryExplicitCancellation',outcomes
        assert threads['Q'].is_alive(), outcomes
        assert not cached.is_closed()
        release.set()
        threads['Q'].join(2)
        assert outcomes['Q']=='fixture-Q'
        assert sorted(paths)==['A','B','Q'], 'retry or fallback bypass'
    finally:
        release.set()
        for thread in threads.values(): thread.join(2)
        server.shutdown(); server.server_close(); cached.close()
