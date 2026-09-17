"""Private, bounded extraction failures: exercise parent, child and persisted audit."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def load_plugin():
    path = ROOT / 'plugins/completion-gate'
    spec = importlib.util.spec_from_file_location('cg_reliability', path / '__init__.py', submodule_search_locations=[str(path)])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, sys.modules[spec.name + '.extraction']


def test_error_audit_migrates_and_never_copies_child_text(tmp_path, caplog):
    plugin, extraction = load_plugin()
    secret = 'PRIVATE_ANSWER_REFERENCE_AND_MESSAGE'
    path = tmp_path / 'gate.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY,created REAL,profile TEXT,task_id TEXT,turn_id TEXT,action TEXT,claims TEXT,block_count INTEGER,reason_hashes TEXT,escalation TEXT)')
        db.execute("INSERT INTO events VALUES(1,0,'p','old','t','deliver','[]',0,'[]','null')")
    def broken(_):
        raise subprocess.CalledProcessError(7, [secret], output=secret,
            stderr=f'Traceback: {secret}\nopenai.AuthenticationError: {secret}\n')
    gate = plugin.Gate({'enabled': True, 'db_path': str(path)}, extract=broken)
    assert gate.evaluate(secret, profile='p', task_id='t', turn_id='u') is None
    rows = gate.events()
    diag = rows[-1].get('diagnostics', {})
    assert diag.get('child_exit_code') == 7
    assert diag.get('exception_class') == 'AuthenticationError'
    assert diag.get('cause') == 'authentication'
    assert diag['elapsed_s'] >= 0
    assert rows[0]['action'] == 'deliver'
    assert secret not in json.dumps(rows) + caplog.text
    assert secret.encode() not in path.read_bytes()
    # An exception name itself can contain data; unknown classes never get logged.
    def arbitrary(_):
        raise type(secret, (Exception,), {})(secret)
    gate.extract = arbitrary
    gate.evaluate(secret, profile='p', task_id='t', turn_id='v')
    assert gate.events()[-1]['diagnostics']['exception_class'] == 'unknown'
    assert secret not in caplog.text


def test_transient_retry_uses_remaining_total_budget_only(tmp_path, monkeypatch):
    import types
    plugin, extraction = load_plugin()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('{"auxiliary":{"transient_retries":1}}')
    now = [0.0]
    monkeypatch.setattr(extraction, 'time', types.SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    calls = []
    def child(command, **kw):
        calls.append(kw)
        now[0] += kw['timeout']
        raise subprocess.TimeoutExpired(command, kw['timeout'], stderr='PRIVATE')
    monkeypatch.setattr(extraction.subprocess, 'run', child)
    try:
        extraction.bounded_extract('PRIVATE', timeout=25, deadline=40)
    except Exception:
        pass
    assert len(calls) == 2
    assert [c['timeout'] for c in calls] == [25, 15]
    assert now[0] <= 40
    assert all(0 < json.loads(c['input'])['timeout'] < c['timeout'] for c in calls)
    # A fast recoverable exit succeeds on the one retry; permanent failures do not retry.
    now[0] = 0
    calls.clear()
    def recover(command, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command, stderr='APIConnectionError: PRIVATE')
        return subprocess.CompletedProcess(command, 0, stdout='[]', stderr='')
    monkeypatch.setattr(extraction.subprocess, 'run', recover)
    assert extraction.bounded_extract('PRIVATE', timeout=25, deadline=40) == []
    assert len(calls) == 2
    calls.clear()
    def permanent(command, **kw):
        calls.append(kw)
        raise subprocess.CalledProcessError(1, command, stderr='AuthenticationError: PRIVATE')
    monkeypatch.setattr(extraction.subprocess, 'run', permanent)
    try:
        extraction.bounded_extract('PRIVATE', timeout=25, deadline=40)
    except Exception:
        pass
    assert len(calls) == 1
    calls.clear()
    now[0] = 0
    monkeypatch.setattr(extraction.subprocess, 'run', child)
    try:
        extraction.bounded_extract('PRIVATE', timeout=0.0005, deadline=0.0005)
    except Exception:
        pass
    assert len(calls) == 1
    assert 0 < json.loads(calls[0]['input'])['timeout'] < calls[0]['timeout']


def test_native_child_reports_timeout_route_before_parent_kill(tmp_path, monkeypatch, caplog):
    import hashlib
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import pytest
    plugin, extraction = load_plugin()
    release = threading.Event()
    reached = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            reached.set()
            release.wait(15)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(json.dumps({'auxiliary': {'transient_retries': 0, 'completion_gate': {
        'provider': 'custom', 'model': 'local-test', 'base_url': f'http://127.0.0.1:{server.server_port}/v1'}}}))
    try:
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            extraction.bounded_extract('PRIVATE_TIMEOUT_ANSWER', timeout=12)
        diag = getattr(caught.value, 'diagnostics', {})
        assert diag.get('cause') == 'aux_timeout'
        assert diag['child_exit_code'] == 1  # child exited itself; not TimeoutExpired
        assert diag['aux_provider'] == 'custom'
        assert diag['aux_model_sha256'] == hashlib.sha256(b'local-test').hexdigest()
        assert reached.is_set()
        assert time.monotonic() - started < 14  # scheduling tolerance, not a new budget
        assert 'PRIVATE_TIMEOUT_ANSWER' not in caplog.text
        for log in tmp_path.glob('logs/*.log'):
            assert 'PRIVATE_TIMEOUT_ANSWER' not in log.read_text()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_filtered_native_response_is_classified_without_retry(tmp_path, monkeypatch):
    import types
    import pytest
    from agent import auxiliary_client
    plugin, extraction = load_plugin()
    calls = []
    def filtered(**kwargs):
        calls.append(kwargs)
        if kwargs.get('route_info') is not None:
            kwargs['route_info'].update(provider='anthropic', model='test-model')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            finish_reason='content_filter', message=types.SimpleNamespace(content=None))])
    monkeypatch.setattr(auxiliary_client, 'call_llm', filtered)
    with pytest.raises(Exception) as caught:
        extraction.extract('PRIVATE_FILTERED_ANSWER')
    assert type(caught.value).__name__ == 'ContentFiltered'
    diag = sys.modules[plugin.__name__ + '.diagnostics'].failure(caught.value)
    assert diag['cause'] == 'content_filtered'
    assert diag['cause'] not in sys.modules[plugin.__name__ + '.diagnostics'].TRANSIENT
    assert len(calls) == 1


def test_extractor_child_uses_served_profile_credentials_a_b_a(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    plugin, extraction = load_plugin()
    a, b = tmp_path / 'a', tmp_path / 'b'
    for home, value in ((a, 'synthetic-a'), (b, 'synthetic-b')):
        home.mkdir()
        (home / '.env').write_text(f'ANTHROPIC_API_KEY={value}\n')
        (home / 'config.yaml').write_text('{}')
    monkeypatch.setenv('HERMES_HOME', str(a))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-a')
    observed = []
    def child(command, **kwargs):
        observed.append((kwargs['env']['HERMES_HOME'], kwargs['env'].get('ANTHROPIC_API_KEY')))
        return subprocess.CompletedProcess(command, 0, stdout='[]', stderr='')
    monkeypatch.setattr(extraction.subprocess, 'run', child)
    for home in (a, b, a):
        token = set_hermes_home_override(home)
        try:
            assert extraction.bounded_extract('plan', timeout=10) == []
        finally:
            reset_hermes_home_override(token)
    assert observed == [(str(a), 'synthetic-a'), (str(b), 'synthetic-b'), (str(a), 'synthetic-a')]
