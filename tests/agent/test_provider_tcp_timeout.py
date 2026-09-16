"""Real numeric-address TCP timeout oracle; not a DNS or universal bound.

TEST-NET-1 must genuinely time out. Immediate routing failures are inconclusive,
not green. Observer never delays/releases/replaces native connect behavior.
"""
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from httpcore._backends.sync import SyncBackend
from openai import OpenAI

from agent import auxiliary_client as aux, auxiliary_control as control, provider_control as pc
from agent.process_bootstrap import build_keepalive_http_client


def test_native_15s_tcp_drain_repeated_with_shared_pool_sibling(tmp_path, monkeypatch):
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / 'holds.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = pc.Policy(db, ('openai-codex',))
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    sibling_entered, sibling_release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            sibling_entered.set()
            if not sibling_release.wait(25):
                return
            body = json.dumps(dict(id='local', object='chat.completion', created=0, model='fixture',
                choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='sibling-ok'))])).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    blackhole_url = 'http://192.0.2.1:80'
    sibling_url = f'http://127.0.0.1:{server.server_port}'
    sdk = OpenAI(api_key='synthetic', base_url=blackhole_url, max_retries=0,
                 http_client=build_keepalive_http_client(blackhole_url))
    sibling = OpenAI(api_key='synthetic', base_url=sibling_url, max_retries=0,
                     http_client=build_keepalive_http_client(sibling_url))
    native = SyncBackend.connect_tcp
    calls, failures, facts = [], [], []
    entered = threading.Event()
    def observe(self, host, port, timeout=None, **kwargs):
        if host == '192.0.2.1':
            calls.append(dict(host=host, port=port, timeout=timeout, entered=time.monotonic()))
            entered.set()
            try:
                return native(self, host, port, timeout=timeout, **kwargs)
            except BaseException as exc:
                failures.append(dict(type=type(exc).__name__, elapsed=time.monotonic()-calls[-1]['entered']))
                raise
        return native(self, host, port, timeout=timeout, **kwargs)
    monkeypatch.setattr(SyncBackend, 'connect_tcp', observe)
    try:
        assert sdk.timeout.connect == 15.0
        for cycle in range(2):
            with sqlite3.connect(db) as conn:
                conn.execute('DELETE FROM holds')
            entered.clear(); sibling_entered.clear(); sibling_release.clear()
            results, sibling_results, errors = [], [], []
            attempt = control.Attempt(sdk, 'openai-codex', policy)
            assert attempt.wire.timeout.connect == 15.0
            # SharedTransport wrappers are distinct but own the same inner pool.
            assert attempt.wire._client._transport._inner is sibling._client._transport._inner
            def request(_):
                try:
                    return attempt.client.chat.completions.create(model='fixture', messages=[])
                except BaseException as exc:
                    errors.append(type(exc).__name__)
                    raise
            def owner():
                try:
                    with attempt.registered():
                        aux._run_protected_sync_provider_call(request, {}, attempt=attempt)
                    results.append('late-commit')
                except BaseException as exc:
                    results.append(type(exc).__name__)
            def other_owner():
                try:
                    response = aux._relay_sync_completion(sibling, dict(model='fixture', messages=[]), provider='xai')
                    sibling_results.append(response.choices[0].message.content)
                except BaseException as exc:
                    sibling_results.append(type(exc).__name__)
            other = threading.Thread(target=other_owner, daemon=True)
            thread = threading.Thread(target=owner, daemon=True)
            other.start()
            assert sibling_entered.wait(15), sibling_results
            thread.start()
            try:
                assert entered.wait(15), results
                start = calls[-1]['entered']
                assert calls[-1]['timeout'] == 15.0
                with sqlite3.connect(db) as conn:
                    conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex', 1, f'fixture-{cycle}'))
                thread.join(3)
                assert results == ['AuxiliaryExplicitCancellation'], results
                assert not attempt.worker_ended, 'INCONCLUSIVE: route returned before timeout-drain observation'
                assert id(attempt) in control._ATTEMPTS
                assert other.is_alive() and not sibling.is_closed()
                sibling_release.set()
                other.join(3)
                assert sibling_results == ['sibling-ok'], sibling_results
                deadline = start + 18.0  # declared 3s scheduling margin, single address
                while not attempt.worker_ended and time.monotonic() < deadline:
                    time.sleep(.02)
                receipt = dict(cycle=cycle, effective_timeout=calls[-1]['timeout'],
                    elapsed=time.monotonic()-start, worker_ended=attempt.worker_ended,
                    registry_retained=id(attempt) in control._ATTEMPTS,
                    wrapper_closed=attempt.wire.is_closed(), tcp_force_closed=attempt.tcp_force_closed,
                    owner=results, provider=errors, native_failures=list(failures), sibling=sibling_results)
                facts.append(receipt)
                print(json.dumps(receipt, sort_keys=True), flush=True)
                assert attempt.worker_ended and receipt['elapsed'] <= 18.0, receipt
                assert failures[-1]['type'] == 'ConnectTimeout', f'INCONCLUSIVE: not native timeout: {receipt}'
                assert 14.5 <= failures[-1]['elapsed'] <= 18.0, receipt
                assert errors == ['APITimeoutError']
                assert id(attempt) not in control._ATTEMPTS and attempt.wire.is_closed()
                assert results == ['AuxiliaryExplicitCancellation']
                assert len(calls) == cycle + 1, 'unexpected retry/address attempt'
                assert not sdk.is_closed() and not sibling.is_closed()
            finally:
                sibling_release.set()
                thread.join(3); other.join(3)
        assert not control._ATTEMPTS
    finally:
        (tmp_path / 'tcp-timeout-receipt.json').write_text(json.dumps(facts, indent=2))
        sibling_release.set()
        server.shutdown(); server.server_close()
        sdk.close(); sibling.close()
