"""Synthetic answers; real native SQLite hold/write ordering."""
import multiprocessing as mp
import sqlite3
import threading

import pytest

from agent import provider_control as pc
from agent.context_compressor import _DB_PERSISTED_MARKER
from hermes_state import SessionDB
from tests.agent.test_load_time_durability_stamp_92231 import _make_flush_agent


def hold_process(path, attempted, committed, ready, go):
    ready.set()
    if not go.wait(30):
        return
    with sqlite3.connect(path, timeout=5) as conn:
        attempted.set()
        conn.execute('INSERT INTO holds VALUES(?)', ('anthropic',))
    committed.set()


@pytest.mark.parametrize('separate_process', [False, True])
def test_native_commit_first_serializes_hold(tmp_path, monkeypatch, separate_process):
    control = tmp_path / 'control.db'
    with sqlite3.connect(control) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT)')
    policy = pc.Policy(control, ['anthropic'])
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    db = SessionDB(db_path=tmp_path/'session.db')
    db.create_session('s', source='cli')
    agent = _make_flush_agent(db, 's')
    agent.provider = 'anthropic'
    messages = [dict(role='user', content='input'), dict(role='assistant', content='pre-hold')]
    ctx = mp.get_context('spawn')
    event_type = ctx.Event if separate_process else threading.Event
    attempted, committed = event_type(), event_type()
    ready, go = event_type(), event_type()
    worker_type = ctx.Process if separate_process else threading.Thread
    worker = worker_type(target=hold_process, args=(str(control), attempted, committed, ready, go))
    worker.start()
    assert ready.wait(30)  # cold imports are setup, never part of the ordering oracle
    original = db._insert_message_rows
    def ordered_write(*a, **kw):
        go.set()
        assert attempted.wait(5)
        # A second writer cannot acquire the reservation while native commit runs.
        with sqlite3.connect(control, timeout=0) as probe:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                probe.execute('BEGIN IMMEDIATE')
        assert not committed.is_set()
        result = original(*a, **kw)
        assert not committed.is_set()
        return result
    monkeypatch.setattr(db, '_insert_message_rows', ordered_write)
    try:
        assert agent._flush_messages_to_session_db(messages, []) is True
        worker.join(5)
        assert not worker.is_alive() and committed.is_set()
        assert all(m.get(_DB_PERSISTED_MARKER) for m in messages)
        # Pre-hold history is retained; later completion is rejected on reopened state.
        monkeypatch.setattr(db, '_insert_message_rows', original)
        late = messages + [dict(role='assistant', content='forbidden')]
        with pytest.raises(pc.HeldProvider):
            agent._flush_messages_to_session_db(late, [])
        assert 'forbidden' not in repr(late)
        assert agent._flush_messages_to_session_db(late, []) is True
        assert len(db.get_messages('s')) == 2
    finally:
        go.set()
        if worker.ident is not None:
            worker.join(5)
        db.close()
    reopened = SessionDB(db_path=tmp_path/'session.db')
    try:
        assert [m['content'] for m in reopened.get_messages('s')] == ['input', 'pre-hold']
    finally:
        reopened.close()
