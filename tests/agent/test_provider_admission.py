"""Dispatch gates exercise native functions with temporary durable holds."""
import sqlite3
from types import SimpleNamespace
from agent import provider_control as pc


def policy_fixture(tmp_path, monkeypatch):
    from hermes_cli import config
    db = tmp_path/'control.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
        conn.execute('INSERT INTO holds VALUES(?,?,?)', ('anthropic', 1, 'synthetic'))
    policy = pc.Policy(db, ('anthropic','openai-codex'))
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    monkeypatch.setattr(config, 'load_config_readonly', lambda: dict(model=dict(provider='anthropic')))
    return policy


def test_kanban_held_profile_stays_unclaimed_even_with_override(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    policy_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda name: True)
    result = SimpleNamespace(respawn_guarded=[])
    consumed = dispatch._dispatch_lane_task(None, dict(id='fixture',provider='openai-codex'), 'fixture', result,
        lane='ready', dry_run=False, ttl_seconds=None, board=None, failure_limit=2,
        spawn_fn=lambda *a: (_ for _ in ()).throw(AssertionError('must not spawn')), per_profile_cap=None,
        per_profile_running={})
    assert consumed is False
    assert result.respawn_guarded == [('fixture','provider held')]


def test_cron_hold_gate_exempts_no_agent(tmp_path, monkeypatch):
    from cron import scheduler
    policy_fixture(tmp_path, monkeypatch)
    reached = []
    # Stop after admission, before any native scheduling/state write.
    monkeypatch.setattr(scheduler, '_interpreter_shutting_down', lambda: reached.append('post-admission') or True)
    assert scheduler._submit_with_guard(dict(id='fixture',no_agent=False), None, None) is None
    assert reached == []
    assert scheduler._submit_with_guard(dict(id='fixture',no_agent=True), None, None) is None
    assert reached == ['post-admission']
