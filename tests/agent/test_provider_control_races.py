"""Deterministic race barriers; transport closure is tested separately on sockets."""
import sqlite3
import pytest
from openai import OpenAI
from agent import auxiliary_client as aux, provider_control as pc
from agent.auxiliary_control import Attempt, _ATTEMPTS


@pytest.mark.parametrize('edge', ['before-register', 'after-register', 'late-result'])
def test_hold_race_discards_result_and_unregisters(tmp_path, monkeypatch, edge):
    db = tmp_path/'race.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = pc.Policy(db, ('openai-codex',))
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    def hold():
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('openai-codex',1,'synthetic'))
    original = OpenAI(api_key='synthetic-not-a-secret', base_url='http://127.0.0.1:1', max_retries=0)
    attempt = Attempt(original, 'openai-codex', policy)
    called = []
    def callback(request):
        called.append('provider-returned')
        hold()
        return 'must-not-commit'
    try:
        with pytest.raises((pc.HeldProvider, aux.AuxiliaryExplicitCancellation)):
            if edge == 'before-register':
                hold()
            with attempt.registered():
                if edge == 'after-register':
                    hold()
                aux._run_protected_sync_provider_call(callback, {}, attempt=attempt)
        assert id(attempt) not in _ATTEMPTS
        assert attempt.wire.is_closed()
        assert called == (['provider-returned'] if edge == 'late-result' else [])
    finally:
        original.close()
