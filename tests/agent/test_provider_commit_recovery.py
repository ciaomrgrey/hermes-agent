"""Actual native SQLite failure, scope and crash/reopen oracles."""
import multiprocessing as mp
import os
import sqlite3
import time

import pytest

from agent import provider_control as pc
from hermes_state import SessionDB
from tests.agent.test_load_time_durability_stamp_92231 import _make_flush_agent


def crash_writer(control, state):
    pc.current_policy = lambda: pc.Policy(control, ['anthropic'])
    db = SessionDB(db_path=state)
    agent = _make_flush_agent(db, 's')
    agent.provider = 'anthropic'
    class CrashAfterCommit:
        def __getattr__(self, name):
            return getattr(native, name)
        def commit(self):
            native.commit()
            os._exit(0)  # actual crash before reservation exit/marker stamping
    native = db._conn
    db._conn = CrashAfterCommit()
    agent._flush_messages_to_session_db([dict(role='assistant', content='pre-crash')], [])


def test_crash_after_session_commit_releases_control_reservation(tmp_path, monkeypatch):
    control, state = tmp_path/'control.db', tmp_path/'state.db'
    with sqlite3.connect(control) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT)')
    db = SessionDB(db_path=state)
    db.create_session('s', source='cli')
    db.close()
    process = mp.get_context('spawn').Process(target=crash_writer, args=(control, state))
    process.start()
    process.join(10)
    assert not process.is_alive() and process.exitcode == 0
    with sqlite3.connect(control, timeout=1) as conn:
        conn.execute('INSERT INTO holds VALUES(?)', ('anthropic',))
    policy = pc.Policy(control, ['anthropic'])
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    db = SessionDB(db_path=state)
    agent = _make_flush_agent(db, 's')
    agent.provider = 'anthropic'
    try:
        messages = db.get_messages_as_conversation('s')
        assert [m['content'] for m in messages] == ['pre-crash']
        messages += [dict(role='user', content='pending'), dict(role='assistant', content='forbidden')]
        with pytest.raises(pc.HeldProvider):
            agent._flush_messages_to_session_db(messages, [])
        assert agent._flush_messages_to_session_db(messages, []) is True
        assert [m['content'] for m in db.get_messages('s')] == ['pre-crash', 'pending']
        import argparse
        from hermes_cli.subcommands.pause import build_pause_parser
        parser = argparse.ArgumentParser()
        build_pause_parser(parser.add_subparsers())
        args = parser.parse_args(['resume', '--provider', 'anthropic'])
        assert args.func(args) == 0  # native explicit resume; no automatic inference/replay
        assert agent._flush_messages_to_session_db(messages, []) is True
        assert len(db.get_messages('s')) == 2
    finally:
        db.close()


@pytest.mark.parametrize('state_kind', ['missing', 'malformed', 'locked', 'held'])
def test_control_failure_rejects_answer_preserves_input(tmp_path, monkeypatch, state_kind):
    control = tmp_path/'control.db'
    locker = None
    if state_kind != 'missing':
        with sqlite3.connect(control) as conn:
            conn.execute('CREATE TABLE holds(provider TEXT)')
            if state_kind == 'held':
                conn.execute('INSERT INTO holds VALUES(?)', ('anthropic',))
            if state_kind == 'malformed':
                conn.execute('DROP TABLE holds')
        if state_kind == 'locked':
            locker = sqlite3.connect(control)
            locker.execute('BEGIN IMMEDIATE')
    policy = pc.Policy(control, ['anthropic'])
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    db = SessionDB(db_path=tmp_path/'state.db')
    db.create_session('s', source='cli')
    agent = _make_flush_agent(db, 's')
    agent.provider = 'anthropic'
    messages = [dict(role='user', content='pending'), dict(role='assistant', content='forbidden')]
    try:
        start = time.monotonic()
        with pytest.raises(pc.HeldProvider):
            agent._flush_messages_to_session_db(messages, [])
        assert time.monotonic()-start < 3
        assert [m['content'] for m in db.get_messages('s')] == ['pending']
        assert 'forbidden' not in repr(agent._session_messages)
        # Unaffected provider can use its separate session while control is locked.
        db.create_session('sibling', source='cli')
        sibling = _make_flush_agent(db, 'sibling')
        sibling.provider = 'xai'
        assert sibling._flush_messages_to_session_db([dict(role='assistant', content='allowed')], []) is True
    finally:
        if locker:
            locker.close()
        db.close()
