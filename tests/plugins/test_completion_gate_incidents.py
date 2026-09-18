"""Regressions grounded in Plutus mirror5933 and sweep Telegram receipt4635.

No estate writes, provider requests, or outbound sends.
"""
import sqlite3
from tests.plugins.test_completion_gate_health import checker, config


def test_mirror_provenance_survives_native_sqlite_and_is_not_a_final(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_state_registry import acquire, release_or_close
    from gateway.mirror import mirror_to_session
    home = tmp_path / 'profiles' / 'plutus'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = acquire()
    try:
        db.create_session('slack', source='slack', model='test')
        # Real mirror path; fixture content is synthetic, incident timestamp is recorded.
        assert mirror_to_session('slack', 'test', 'outbound bridge report', session_id='slack')
        with sqlite3.connect(home / 'state.db') as conn:
            conn.execute('UPDATE messages SET timestamp=1789724685.88437')
            assert conn.execute('SELECT display_kind FROM messages').fetchone()[0] == 'delivery_mirror'
        health = checker()
        state = tmp_path / 'health.db'
        assert not health.activity_gap(home, tmp_path / 'absent.db', state,
                                       now=1789725041.56652, tolerance=100)
        assert health.post_update_check([home], tmp_path / 'absent.db',
                                        now=1789725041.56652)['reasons'] == ['no_recent_reply_evidence']
        # Unknown/ordinary assistant output must NOT be waived by null finish_reason.
        db.append_message('slack', 'assistant', content='ordinary final')
        with sqlite3.connect(home / 'state.db') as conn:
            conn.execute('UPDATE messages SET timestamp=1789725080 WHERE display_kind IS NULL')
        assert health.activity_gap(home, tmp_path / 'absent.db', state,
                                   now=1789725200, tolerance=100)
        # Worker final5955 precedes actual gate event442 by 17.09s.
        gate_path = tmp_path / 'gate.db'
        with sqlite3.connect(gate_path) as gate:
            gate.execute('CREATE TABLE events(created REAL, profile TEXT)')
            gate.execute("INSERT INTO events VALUES(1789724797.19179, 'plutus')")
        with sqlite3.connect(home / 'state.db') as conn:
            conn.execute('UPDATE messages SET timestamp=1789724780.09828 WHERE display_kind IS NULL')
        assert not health.activity_gap(home, gate_path, tmp_path / 'worker-health.db',
                                       now=1789725041.56652, tolerance=100)
    finally:
        release_or_close(db)


def test_confirmed_alarm_deduplicates_but_failed_delivery_retries(tmp_path):
    health = checker()
    home = tmp_path / 'profile'
    config(home, enabled=False)
    state = tmp_path / 'health.db'
    calls = []
    responses = iter([{'status': 'unavailable'}, {'status': 'sent', 'transport': 'telegram'}])
    def send(event):
        calls.append(event)
        return next(responses)
    assert health.check(home, state, now=1000, send=send)['delivery']['status'] == 'unavailable'
    assert health.check(home, state, now=1100, send=send)['delivery']['status'] == 'sent'
    # New module instance simulates next cron process, not an in-memory debounce.
    result = checker().check(home, state, now=1200, send=send)
    assert result['ok'] is False
    assert result['delivery']['status'] == 'already_sent'
    assert len(calls) == 2
    health.authorize_disable(home, state, reference='test-only', expires=1400)
    assert health.check(home, state, now=1300, send=send)['ok']
    fresh = health.check(home, state, now=1500, send=lambda e: calls.append(e) or {'status': 'sent'})
    assert fresh['delivery']['status'] == 'sent'
    assert len(calls) == 3
