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


@pytest.mark.parametrize('wire', ['openai-block', 'openai-sse', 'codex', 'anthropic', 'main-block', 'main-sse', 'main-inline', 'main-inline-sse', 'async-openai', 'async-codex', 'async-anthropic', 'main-anthropic', 'main-sse-anthropic', 'main-inline-anthropic', 'main-inline-sse-anthropic', 'raw-openai-sse'])
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
                if wire.endswith('anthropic'):
                    setattr(agent, 'provider', provider)
                    agent.api_mode = 'anthropic_messages'
                    agent._anthropic_api_key, agent._anthropic_base_url = 'synthetic-not-a-secret', url
                    agent._try_refresh_anthropic_client_credentials = lambda: False
                if 'inline' in wire:
                    agent.platform = 'cron'
                with policy.track(agent):
                    call = agent._interruptible_streaming_api_call if 'sse' in wire else agent._interruptible_api_call
                    call(dict(model='fixture', max_tokens=16, messages=[dict(role='user',content='local only')]))
                outcomes.append('late-result')
                return
            with aux.aux_progress_hook((lambda: None) if wire == 'openai-sse' else None):
                request = dict(model='fixture', max_tokens=16, messages=[dict(role='user',content='local only')])
                if wire == 'raw-openai-sse':
                    request['stream'] = True
                response = aux._relay_sync_completion(client, request, provider=provider)
                if wire == 'raw-openai-sse':
                    for _ in response:
                        pass
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


@pytest.mark.parametrize('mode', ['sync-idle', 'sync-direct', 'async-idle', 'async-read'])
def test_returned_stream_lifetime_hold(hang_server, tmp_path, monkeypatch, mode):
    import asyncio
    import sqlite3
    from agent import auxiliary_control as control
    url, entered, peer_closed, paths = hang_server
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = provider_control.Policy(db, ('openai-codex',))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: policy)
    def hold():
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'synthetic'))
    request = dict(model='fixture', messages=[], stream=True)
    if mode.startswith('sync-'):
        client = OpenAI(api_key='synthetic', base_url=url, max_retries=0,
                        http_client=build_keepalive_http_client(url), timeout=5)
        try:
            call = aux._relay_sync_stream if mode == 'sync-direct' else aux._relay_sync_completion
            stream = call(client, request, provider='openai-codex')
            assert entered.wait(2)
            hold()
            assert peer_closed.wait(2), 'idle stream socket remains open'
            with pytest.raises(aux.AuxiliaryExplicitCancellation):
                next(iter(stream))
            stream.close()
            stream.close()
            assert not control._ATTEMPTS
            assert not client.is_closed()
        finally:
            client.close()
    else:
        from openai import AsyncOpenAI
        async def run():
            client = AsyncOpenAI(api_key='synthetic', base_url=url, max_retries=0,
                                 http_client=build_keepalive_http_client(url, async_mode=True), timeout=5)
            try:
                stream = await aux._relay_async_completion(client, request, provider='openai-codex')
                reader = asyncio.create_task(anext(stream)) if mode == 'async-read' else None
                await asyncio.sleep(.04)
                hold()
                assert await asyncio.to_thread(peer_closed.wait, 2), 'async stream socket remains open'
                with pytest.raises(aux.AuxiliaryExplicitCancellation):
                    if reader is not None:
                        await reader
                    else:
                        await anext(stream)
                await stream.close()
                await stream.close()
                assert not control._ATTEMPTS
                assert not client.is_closed()
            finally:
                await client.close()
        asyncio.run(run())
    assert paths == ['/chat/completions']


def test_explicit_close_interrupts_blocked_raw_reader(hang_server, tmp_path, monkeypatch):
    import sqlite3
    url, entered, peer_closed, paths = hang_server
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = provider_control.Policy(db, ('openai-codex',))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: policy)
    client = OpenAI(api_key='synthetic', base_url=url, max_retries=0, timeout=5)
    stream = aux._relay_sync_completion(client, dict(model='fixture', messages=[], stream=True), provider='openai-codex')
    reading = threading.Event()
    outcomes = []
    def read():
        reading.set()
        try:
            next(stream)
        except BaseException as exc:
            outcomes.append(type(exc).__name__)
    reader = threading.Thread(target=read, daemon=True)
    closer = threading.Thread(target=stream.close, daemon=True)
    reader.start()
    assert reading.wait(2)
    closer.start()
    try:
        assert peer_closed.wait(2), 'explicit close blocked behind hung reader'
        closer.join(2)
        reader.join(2)
        assert not closer.is_alive() and not reader.is_alive()
        assert len(outcomes) == 1
        assert not stream.observer.is_alive()
    finally:
        closer.join(6)
        reader.join(6)
        client.close()


@pytest.mark.parametrize('async_mode', [False, True])
def test_active_fallback_retains_original_hold(hang_server, tmp_path, monkeypatch, async_mode):
    import asyncio
    import sqlite3
    from types import SimpleNamespace
    url, entered, peer_closed, paths = hang_server
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = provider_control.Policy(db, ('openai-codex',))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: policy)
    outcomes = []
    def owner():
        with policy.track(SimpleNamespace(provider='openai-codex')):
            try:
                if async_mode:
                    from openai import AsyncOpenAI
                    async def run():
                        client = AsyncOpenAI(api_key='synthetic', base_url=url, max_retries=0, timeout=5)
                        try:
                            return await aux._relay_async_completion(client, dict(model='fixture',messages=[]),provider='xai')
                        finally:
                            await client.close()
                    asyncio.run(run())
                else:
                    client = OpenAI(api_key='synthetic', base_url=url, max_retries=0, timeout=5)
                    try:
                        aux._relay_sync_completion(client, dict(model='fixture',messages=[]),provider='xai')
                    finally:
                        client.close()
                outcomes.append('late-result')
            except BaseException as exc:
                outcomes.append(type(exc).__name__)
    worker = threading.Thread(target=owner, daemon=True)
    worker.start()
    try:
        assert entered.wait(15)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'synthetic'))
        assert peer_closed.wait(2), 'fallback continued under original provider hold'
        worker.join(2)
        assert not worker.is_alive()
        assert outcomes == ['AuxiliaryExplicitCancellation']
        assert len(paths) == 1
    finally:
        worker.join(6)


def _cross_process_request(url, database, result_pipe):
    """Spawned process owns its client; parent only writes durable control."""
    from pathlib import Path
    policy = provider_control.Policy(Path(database), ('openai-codex',))
    provider_control.current_policy = lambda: policy
    client = OpenAI(api_key='synthetic-not-a-secret', base_url=url, max_retries=0,
                    http_client=build_keepalive_http_client(url), timeout=5)
    result_pipe.send('ready')
    try:
        aux._relay_sync_completion(client, dict(model='fixture', messages=[]), provider='openai-codex')
        result_pipe.send('late-result')
    except BaseException as exc:
        result_pipe.send(type(exc).__name__)
    finally:
        client.close()
        result_pipe.close()


def test_other_process_observes_hold_and_closes_own_socket(hang_server, tmp_path):
    import multiprocessing
    import sqlite3
    url, entered, closed, paths = hang_server
    db = tmp_path/'multiprocess.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_cross_process_request, args=(url, str(db), send))
    process.start()
    send.close()
    try:
        assert receive.poll(45), 'child setup did not become ready'
        assert receive.recv() == 'ready'
        assert entered.wait(15)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'synthetic'))
        assert closed.wait(2), 'other-process provider socket survived durable hold'
        assert receive.poll(2)
        assert receive.recv() == 'AuxiliaryExplicitCancellation'
        process.join(2)
        assert process.exitcode == 0
        assert len(paths) == 1
    finally:
        process.join(7)
        if process.is_alive():
            process.terminate()  # local synthetic fixture only
            process.join(2)
        receive.close()


@pytest.mark.parametrize('wire', ['aux-openai', 'main-inline-anthropic'])
def test_hold_during_tls_handshake_closes_peer(tmp_path, monkeypatch, wire):
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
    provider = 'anthropic' if wire.endswith('anthropic') else 'openai-codex'
    policy = provider_control.Policy(db, (provider,))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: policy)
    client = OpenAI(api_key='synthetic-not-a-secret', base_url=url, max_retries=0, timeout=5,
                    http_client=build_keepalive_http_client(url, verify=False))
    outcomes = []
    def owner():
        try:
            if wire == 'main-inline-anthropic':
                from tests.run_agent.test_openai_client_lifecycle import _build_agent
                agent = _build_agent(client)
                agent.provider, agent.api_mode, agent.platform = provider, 'anthropic_messages', 'cron'
                agent._anthropic_api_key, agent._anthropic_base_url = 'synthetic-not-a-secret', url
                agent._try_refresh_anthropic_client_credentials = lambda: False
                with policy.track(agent):
                    agent._interruptible_streaming_api_call(dict(model='fixture', max_tokens=16,
                        messages=[dict(role='user', content='local only')]))
            else:
                aux._relay_sync_completion(client, dict(model='fixture', messages=[]), provider=provider)
        except BaseException as exc:
            outcomes.append(type(exc).__name__)
    worker = threading.Thread(target=owner,daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', (provider,1,'fixture'))
        assert closed.wait(2), 'TLS socket not adopted into pool; native abort missed it'
        worker.join(2)
        assert outcomes == ['HeldProvider' if wire.startswith('main-') else 'AuxiliaryExplicitCancellation']
    finally:
        worker.join(7)
        client.close()
        server.shutdown()
        server.server_close()
