"""Delayed pre-socket connect: no HTTP dispatch after hold, pending worker explicit."""
import socket
import sqlite3
import threading

from openai import OpenAI
from httpcore._backends.sync import SyncBackend
from agent import auxiliary_client as aux, auxiliary_control as control, provider_control as pc


def test_presocket_hold_prevents_late_http_dispatch(tmp_path, monkeypatch, caplog):
    for key in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'):
        monkeypatch.delenv(key, raising=False)
    listener = socket.socket()
    listener.bind(('127.0.0.1',0))
    listener.listen()
    listener.settimeout(8)
    received = []
    peer_done = threading.Event()
    def serve():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(3)
                received.append(conn.recv(8192))
        finally:
            peer_done.set()
    server = threading.Thread(target=serve, daemon=True)
    server.start()
    entered, release, provider_done = threading.Event(), threading.Event(), threading.Event()
    native_connect = SyncBackend.connect_tcp
    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(8)
        return native_connect(self, *args, **kwargs)
    monkeypatch.setattr(SyncBackend, 'connect_tcp', delayed)
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = pc.Policy(db, ('openai-codex',))
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    client = OpenAI(api_key='synthetic',base_url=f'http://127.0.0.1:{listener.getsockname()[1]}',max_retries=0,timeout=5)
    attempt = control.Attempt(client, 'openai-codex', policy)
    outcomes = []
    def call(request):
        try:
            return attempt.client.chat.completions.create(model='fixture',messages=[])
        finally:
            provider_done.set()
    def owner():
        try:
            with attempt.registered():
                aux._run_protected_sync_provider_call(call, {}, attempt=attempt)
            outcomes.append('late-result')
        except BaseException as exc:
            outcomes.append(type(exc).__name__)
    worker = threading.Thread(target=owner, daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'synthetic'))
        worker.join(3)
        assert not worker.is_alive()
        assert outcomes == ['AuxiliaryExplicitCancellation']
        assert not provider_done.is_set(), 'fixture must retain genuinely pending provider worker'
        assert "'worker_ended': False" in caplog.text
        assert "'tcp_force_closed': 0" in caplog.text
        release.set()
        assert peer_done.wait(3)
        assert provider_done.wait(3)
        assert received == [b''], 'late connection sent HTTP after durable hold'
        assert attempt.wire.is_closed()
    finally:
        release.set()
        worker.join(6)
        server.join(4)
        listener.close()
        client.close()
