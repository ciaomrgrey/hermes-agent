"""Native provider control seam; tests use only temporary policy/database."""
import importlib.util
import sqlite3
from types import SimpleNamespace
import pytest


def test_provider_hold_admission_and_active_isolation(tmp_path):
    assert importlib.util.find_spec('agent.provider_control'), 'provider-scoped native control missing'
    from agent.provider_control import Policy, HeldProvider
    db = tmp_path/'state.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT PRIMARY KEY, since REAL, incident TEXT)')
    policy = Policy(db, ('anthropic', 'openai-codex'))
    policy.check('anthropic')
    calls = []
    a = SimpleNamespace(provider='anthropic', interrupt=lambda *args, **kw: calls.append(('a',kw)))
    b = SimpleNamespace(provider='openai-codex', interrupt=lambda *args, **kw: calls.append(('b',kw)))
    with policy.track(a, schedule=False) as a_guard, policy.track(b, schedule=False) as b_guard:
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('anthropic', 1, 'synthetic'))
        with pytest.raises(HeldProvider):
            policy.check('anthropic')
        policy.check('openai-codex')
        a_guard.poll()
        b_guard.poll()
        assert calls == [('a', {'hard_cancel': True, 'propagate_children': False})]
        # Changing runtime to a fallback cannot bypass original provider ownership.
        a.provider = 'openai-codex'
        with pytest.raises(HeldProvider):
            a_guard.check()
    policy.resume('anthropic')
    policy.check('anthropic')


def test_corrupt_control_is_fail_closed_only_for_configured_providers(tmp_path):
    from agent.provider_control import Policy, HeldProvider
    policy = Policy(tmp_path/'missing.sqlite3', ('anthropic', 'openai-codex'))
    with pytest.raises(HeldProvider):
        policy.check('anthropic')
    policy.check('xai-oauth')


def test_native_interrupt_can_cancel_one_agent_without_other_provider_child():
    import threading
    from agent.interrupt_control import InterruptControlMixin
    class Native(InterruptControlMixin):
        pass
    agent = Native()
    agent._hard_interrupt_requested = threading.Event()
    agent._execution_thread_id = None
    agent._active_children_lock = threading.Lock()
    agent.quiet_mode = True
    cancelled = []
    agent._active_request_abort = lambda reason: cancelled.append('request')
    agent._active_children = [SimpleNamespace(hard_interrupt=lambda *a, **kw: cancelled.append('child'))]
    import inspect
    assert 'propagate_children' in inspect.signature(agent.interrupt).parameters, 'native selective cancellation missing'
    agent.interrupt('hold', hard_cancel=True, propagate_children=False)
    assert cancelled == ['request']
    assert agent._hard_interrupt_requested.is_set()


def test_real_turn_facade_refuses_held_provider_before_preflight(tmp_path, monkeypatch):
    from agent.provider_control import Policy, HeldProvider
    from agent.turn_facade import TurnFacadeMixin
    from agent import provider_control
    db = tmp_path/'state.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT PRIMARY KEY, since REAL, incident TEXT)')
        conn.execute('INSERT INTO holds VALUES(?,?,?)', ('anthropic', 1, 'synthetic'))
    monkeypatch.setattr(provider_control, 'current_policy', lambda: Policy(db, ('anthropic',)))
    class Bare(TurnFacadeMixin):
        provider = 'anthropic'
    # Deliberately no other agent attrs: gate must precede auxiliary/preflight work.
    with pytest.raises(HeldProvider):
        Bare().run_conversation('must not reach inference')
