"""Do not retain the global control reservation while native writer waits."""
import sqlite3
import threading

from agent import provider_control as pc
from hermes_state import SessionDB
from tests.agent.test_load_time_durability_stamp_92231 import _make_flush_agent


def test_unaffected_configured_provider_commits_while_native_writer_waits(tmp_path, monkeypatch):
    control = tmp_path/'control.db'
    with sqlite3.connect(control) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT)')
    policy = pc.Policy(control, ['anthropic', 'openai-codex'])
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    db = SessionDB(db_path=tmp_path/'a.db')
    sibling_db = SessionDB(db_path=tmp_path/'b.db')
    db.create_session('a', source='cli')
    sibling_db.create_session('b', source='cli')
    agent, sibling = _make_flush_agent(db, 'a'), _make_flush_agent(sibling_db, 'b')
    agent.provider, sibling.provider = 'anthropic', 'openai-codex'
    entered = threading.Event()
    native = db._execute_write
    def blocked_native(*args, **kwargs):
        entered.set()
        return native(*args, **kwargs)
    monkeypatch.setattr(db, '_execute_write', blocked_native)
    db._lock.acquire()  # genuine native shared writer lock, no interposed wait
    results = []
    def write():
        try:
            results.append(agent._flush_messages_to_session_db([dict(role='assistant', content='a')], []))
        except BaseException as exc:
            results.append(type(exc).__name__)
    worker = threading.Thread(target=write)
    worker.start()
    try:
        assert entered.wait(5)
        # Other provider uses a different session DB; no legitimate data lock conflict.
        assert sibling._flush_messages_to_session_db([dict(role='assistant', content='b')], []) is True
    finally:
        db._lock.release()
        worker.join(5)
        db.close()
        sibling_db.close()
    assert not worker.is_alive() and results == [True]


def test_native_busy_retry_releases_reservation_and_rechecks_hold(tmp_path, monkeypatch):
    control = tmp_path/'control.db'
    with sqlite3.connect(control) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT)')
    policy = pc.Policy(control, ['anthropic'])
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    db = SessionDB(db_path=tmp_path/'state.db')
    db.create_session('s', source='cli')
    agent = _make_flush_agent(db, 's')
    agent.provider = 'anthropic'
    locker = sqlite3.connect(tmp_path/'state.db')
    locker.execute('BEGIN IMMEDIATE')
    retries = []
    import time
    native_conn = db._conn
    attempts = []
    class ObserveBegin:
        def __getattr__(self, name):
            return getattr(native_conn, name)
        def execute(self, sql, *args):
            started = time.monotonic()
            try:
                return native_conn.execute(sql, *args)
            finally:
                if sql == 'BEGIN IMMEDIATE':
                    attempts.append(time.monotonic() - started)
    db._conn = ObserveBegin()
    def retry_after_real_busy(*args):
        # Measure SQLite waiting itself, excluding imports/setup.
        assert attempts[-1] < 0.25
        assert native_conn.execute('PRAGMA busy_timeout').fetchone()[0] == 1000
        # Real native BEGIN has failed on the live SQLite writer reservation.
        # The control reservation must have ended before native retry machinery.
        with sqlite3.connect(control, timeout=0) as conn:
            conn.execute('INSERT INTO holds VALUES(?)', ('anthropic',))
        retries.append('hold_committed')
        locker.rollback()
        return True
    monkeypatch.setattr(db, '_sleep_before_write_retry', retry_after_real_busy)
    messages = [dict(role='user', content='pending'), dict(role='assistant', content='forbidden')]
    try:
        import pytest
        with pytest.raises(pc.HeldProvider):
            agent._flush_messages_to_session_db(messages, [])
        assert retries == ['hold_committed']
        assert [m['content'] for m in db.get_messages('s')] == ['pending']
        assert 'forbidden' not in repr(messages)
    finally:
        locker.close()
        db.close()
