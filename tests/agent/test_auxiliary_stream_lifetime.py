"""Synthetic loopback SSE only; no provider quota or operational holds."""
import asyncio
import json
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from agent import auxiliary_client as aux, auxiliary_control as control, provider_control
from agent.process_bootstrap import build_keepalive_http_client
from openai import OpenAI, AsyncOpenAI


@pytest.fixture
def stream_server(monkeypatch):
    for key in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'):
        monkeypatch.delenv(key, raising=False)
    closed = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            chunk = dict(id='fixture', object='chat.completion.chunk', created=0, model='fixture',
                         choices=[dict(index=0, delta=dict(content='one'), finish_reason=None)])
            payload = ('data: '+json.dumps(chunk)+'\n\n').encode()
            if request['model'] == 'eof':
                payload += b'data: [DONE]\n\n'
            if request['model'] == 'error':
                payload = b'data: invalid-json\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            if request['model'] in ('eof','error'):
                self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            if request['model'] in ('eof','error'):
                return
            self.connection.settimeout(5)
            try:
                if self.connection.recv(1) == b'':
                    closed.set()
            except ConnectionResetError:
                closed.set()
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
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}', closed
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('ending', ['eof', 'early-close', 'error', 'idle-after-chunk'])
def test_stream_semantics_and_cleanup(stream_server, tmp_path, monkeypatch, async_mode, ending):
    url, peer_closed = stream_server
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = provider_control.Policy(db, ('openai-codex',))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: policy)
    request = dict(model=ending, messages=[], stream=True)
    def hold():
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'synthetic'))
    sibling = build_keepalive_http_client(url)
    if not async_mode:
        client = OpenAI(api_key='synthetic', base_url=url, max_retries=0,
                        http_client=build_keepalive_http_client(url), timeout=5)
        try:
            stream = aux._relay_sync_stream(client, request, provider='openai-codex')
            if ending == 'error':
                with pytest.raises(json.JSONDecodeError):
                    next(stream)
            else:
                assert next(stream).choices[0].delta.content == 'one'
                if ending == 'eof':
                    with pytest.raises(StopIteration):
                        next(stream)
                if ending == 'idle-after-chunk':
                    hold()
                    assert peer_closed.wait(2)
                    with pytest.raises(aux.AuxiliaryExplicitCancellation):
                        next(stream)
            stream.close()
            stream.close()
            assert not stream.observer.is_alive()
            assert stream.attempt.wire.is_closed()
            assert not client.is_closed()
        finally:
            client.close()
    else:
        async def run():
            client = AsyncOpenAI(api_key='synthetic', base_url=url, max_retries=0,
                                 http_client=build_keepalive_http_client(url, async_mode=True), timeout=5)
            try:
                stream = await aux._relay_async_completion(client, request, provider='openai-codex')
                if ending == 'error':
                    with pytest.raises(json.JSONDecodeError):
                        await anext(stream)
                else:
                    assert (await anext(stream)).choices[0].delta.content == 'one'
                    if ending == 'eof':
                        with pytest.raises(StopAsyncIteration):
                            await anext(stream)
                    if ending == 'idle-after-chunk':
                        hold()
                        assert await asyncio.to_thread(peer_closed.wait,2)
                        with pytest.raises(aux.AuxiliaryExplicitCancellation):
                            await anext(stream)
                await stream.aclose()
                await stream.aclose()
                assert stream.observer.done()
                assert stream.client.is_closed()
                assert not client.is_closed()
            finally:
                await client.close()
        asyncio.run(run())
    assert not control._ATTEMPTS
    if ending == 'early-close':
        assert peer_closed.wait(2)
    try:
        assert sibling.get(url+'/sibling').text == 'ok'
    finally:
        sibling.close()
