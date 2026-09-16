"""Real local-socket oracle: owner return is not transport cancellation."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from agent import auxiliary_client as aux
from agent import provider_control
from agent.process_bootstrap import build_keepalive_http_client
from openai import OpenAI


@pytest.fixture
def hang_server(monkeypatch):
    for name in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'):
        monkeypatch.delenv(name, raising=False)
    entered, peer_closed = threading.Event(), threading.Event()
    paths = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_POST(self):
            import json
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            paths.append(self.path)
            if payload.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.flush()
            entered.set()
            self.connection.settimeout(4)
            try:
                if self.connection.recv(1) == b'':
                    peer_closed.set()
            except ConnectionResetError:
                peer_closed.set()
            except socket.timeout:
                pass
            self.close_connection = True
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length','2')
            self.end_headers()
            self.wfile.write(b'ok')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    yield url, entered, peer_closed, paths
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('wire', ['openai-block', 'openai-sse', 'codex', 'anthropic', 'main-block', 'main-sse', 'main-inline', 'main-inline-sse', 'async-openai', 'async-codex', 'async-anthropic'])
def test_hold_tears_down_nonstream_socket_and_preserves_sibling(hang_server, tmp_path, monkeypatch, caplog, wire):
    import sqlite3
    url, entered, peer_closed, paths = hang_server
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    provider = 'anthropic' if wire.endswith('anthropic') else 'openai-codex'
    policy = provider_control.Policy(db, (provider,))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: policy)
    if wire.endswith('anthropic'):
        from anthropic import Anthropic
        original = Anthropic(api_key='synthetic-not-a-secret', base_url=url,
                             http_client=build_keepalive_http_client(url), max_retries=0, timeout=5)
        client = aux.AnthropicAuxiliaryClient(original, 'fixture', 'synthetic-not-a-secret', url)
    else:
        original = OpenAI(api_key='synthetic-not-a-secret', base_url=url,
                          http_client=build_keepalive_http_client(url), max_retries=0, timeout=5)
        client = aux.CodexAuxiliaryClient(original, 'fixture') if 'codex' in wire else original
    sibling = build_keepalive_http_client(url)
    outcomes = []
    def owner():
        try:
            if wire == 'async-openai':
                import asyncio
                from openai import AsyncOpenAI
                async def call_async():
                    async_client = AsyncOpenAI(api_key='synthetic-not-a-secret', base_url=url, max_retries=0,
                                               http_client=build_keepalive_http_client(url, async_mode=True))
                    try:
                        return await aux._relay_async_completion(async_client,
                            dict(model='fixture', messages=[dict(role='user', content='local only')]), provider=provider)
                    finally:
                        await async_client.close()
                asyncio.run(call_async())
                outcomes.append('late-result')
                return
            if wire in ('async-codex','async-anthropic'):
                import asyncio
                wrapper = aux.AsyncCodexAuxiliaryClient if wire == 'async-codex' else aux.AsyncAnthropicAuxiliaryClient
                asyncio.run(aux._relay_async_completion(wrapper(client),
                    dict(model='fixture',max_tokens=16,messages=[dict(role='user',content='local only')]),provider=provider))
                outcomes.append('late-result')
                return
            if wire.startswith('main-'):
                from tests.run_agent.test_openai_client_lifecycle import _build_agent
                agent = _build_agent(original)
                agent.base_url = url
                agent._client_kwargs = dict(api_key='synthetic-not-a-secret', base_url=url)
                if 'inline' in wire:
                    agent.platform = 'cron'
                with policy.track(agent):
                    call = agent._interruptible_streaming_api_call if 'sse' in wire else agent._interruptible_api_call
                    call(dict(model='fixture', messages=[dict(role='user',content='local only')]))
                outcomes.append('late-result')
                return
            with aux.aux_progress_hook((lambda: None) if wire == 'openai-sse' else None):
                aux._relay_sync_completion(client, dict(model='fixture', max_tokens=16, messages=[dict(role='user',content='local only')]),
                                           provider=provider)
            outcomes.append('late-result')
        except BaseException as exc:
            outcomes.append(type(exc).__name__)
    worker = threading.Thread(target=owner, daemon=True)
    worker.start()
    try:
        assert entered.wait(15), f'request never reached local server: {outcomes}'
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', (provider,1,'synthetic'))
        assert peer_closed.wait(2), 'owner-only cancel: server still sees open provider socket'
        worker.join(2)
        assert not worker.is_alive(), 'request owner did not finish'
        assert outcomes == ['HeldProvider' if wire.startswith('main-') else 'AuxiliaryExplicitCancellation']
        assert len(paths) == 1, 'retry/fallback escaped hold'
        assert sibling.get(url+'/sibling').text == 'ok'
        assert not original.is_closed(), 'process-shared cached client must not be closed'
        if not wire.startswith(('main-', 'async-')):
            assert "'tcp_force_closed': 1" in caplog.text
            assert "'worker_ended': True" in caplog.text
    finally:
        worker.join(6)
        sibling.close()
        client.close()


def test_hold_during_tls_handshake_closes_peer(tmp_path, monkeypatch):
    """Adversarial prerequisite: HTTP pool has not adopted its network stream yet."""
    import sqlite3
    import socketserver
    entered, closed = threading.Event(), threading.Event()
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            try:
                self.request.recv(65535)  # consume TLS ClientHello, never answer
                entered.set()
                while self.request.recv(1024):
                    pass
                closed.set()
            except ConnectionResetError:
                closed.set()
            except socket.timeout:
                pass
    server = socketserver.ThreadingTCPServer(('127.0.0.1',0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'https://127.0.0.1:{server.server_address[1]}'
    db = tmp_path/'tls.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    monkeypatch.setattr(provider_control, 'current_policy', lambda: provider_control.Policy(db, ('openai-codex',)))
    client = OpenAI(api_key='synthetic-not-a-secret', base_url=url, max_retries=0, timeout=5,
                    http_client=build_keepalive_http_client(url, verify=False))
    outcomes = []
    def owner():
        try:
            aux._relay_sync_completion(client, dict(model='fixture', messages=[]), provider='openai-codex')
        except BaseException as exc:
            outcomes.append(type(exc).__name__)
    worker = threading.Thread(target=owner,daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'fixture'))
        assert closed.wait(2), 'TLS socket not adopted into pool; native abort missed it'
        worker.join(2)
        assert outcomes == ['AuxiliaryExplicitCancellation']
    finally:
        worker.join(7)
        client.close()
        server.shutdown()
        server.server_close()
