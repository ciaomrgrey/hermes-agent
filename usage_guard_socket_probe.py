"""Compare native owner-only cancellation with actual socket termination.
All traffic is loopback. Fixture cleanup happens AFTER receipt capture.
"""
import argparse
import json
import pathlib
import socket
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

p = argparse.ArgumentParser()
p.add_argument('--source', type=pathlib.Path, required=True)
a = p.parse_args()
sys.path.insert(0, str(a.source.resolve()))
from agent import auxiliary_client as aux
from agent.process_bootstrap import build_keepalive_http_client
from openai import OpenAI

entered, closed, cancelled = threading.Event(), threading.Event(), threading.Event()
connections = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        connections.append(self.connection)
        entered.set()
        self.connection.settimeout(10)
        try:
            if self.connection.recv(1) == b'': closed.set()
        except (ConnectionResetError, BrokenPipeError): closed.set()
        except OSError: pass
server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{server.server_port}'
client = OpenAI(api_key='local-fixture', base_url=url, max_retries=0,
                http_client=build_keepalive_http_client(url), timeout=5)
outcomes = []
with tempfile.TemporaryDirectory() as tmp:
    if (a.source/'agent/provider_control.py').exists():
        from agent import provider_control as pc
        db = pathlib.Path(tmp)/'control.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE holds(provider TEXT,since REAL,incident TEXT)')
        pc.current_policy = lambda: pc.Policy(db, ('openai-codex',))
    def owner():
        try:
            with aux.aux_interrupt_protection(cancel_check=cancelled.is_set):
                aux._relay_sync_completion(client, dict(model='fixture', messages=[dict(role='user',content='local only')]), provider='openai-codex')
            outcomes.append('late-result')
        except BaseException as e:
            outcomes.append(type(e).__name__)
    t = threading.Thread(target=owner, daemon=True)
    t.start()
    assert entered.wait(15), outcomes
    cancelled.set()
    t.join(2)
    peer_closed = closed.wait(.2)
    workers_alive = any(t.name == 'hermes-protected-aux-provider' and t.is_alive() for t in threading.enumerate())
    print(json.dumps(dict(source_file=aux.__file__, owner_released=not t.is_alive(), outcomes=outcomes,
        peer_closed_before_fixture_cleanup=peer_closed, provider_worker_alive_before_fixture_cleanup=workers_alive,
        qualified_local_teardown=peer_closed and not workers_alive and not t.is_alive()), indent=2))
    for connection in connections:
        try: connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
    server.shutdown()
    server.server_close()
    client.close()
