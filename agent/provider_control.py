"""Opt-in provider-scoped admission and cooperative hard-cancel control.

Not ESTOP: deterministic no-agent jobs are unaffected. Holds are persistent and
never expire. SQLite is shared with the controller; no second control daemon.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class HeldProvider(InterruptedError):
    """An explicit hold is cancellation, never a retry/fallback candidate."""


_SCOPE: ContextVar[ActiveScope | None] = ContextVar('provider_control_scope', default=None)


class Policy:
    def __init__(self, database, providers):
        self.database = Path(database) if database else None
        self.providers = tuple(providers)

    def check(self, *providers):
        targets = set(providers).intersection(self.providers)
        if not targets or self.database is None:
            return
        try:
            with sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True, timeout=1) as conn:
                held = {r[0] for r in conn.execute('SELECT provider FROM holds')}
        except (sqlite3.Error, OSError):
            raise HeldProvider('Provider control state unavailable; admission refused') from None
        if targets.intersection(held):
            raise HeldProvider('Provider held; explicit operator resume required')

    def resume(self, provider):
        if self.database is None:
            return
        with sqlite3.connect(self.database, timeout=5) as conn:
            conn.execute('DELETE FROM holds WHERE provider=?', (provider,))

    @contextmanager
    def track(self, agent, *, schedule=True):
        scope = ActiveScope(self, agent)
        scope.check()
        token = _SCOPE.set(scope)
        handle = None
        try:
            if schedule and self.database is not None:
                from agent.periodic_scheduler import schedule as native_schedule
                handle = native_schedule(scope.poll, 1.0)
            yield scope
        finally:
            with scope.lock:
                scope.active = False
            if handle is not None:
                handle.cancel(wait=2)
            _SCOPE.reset(token)


class ActiveScope:
    def __init__(self, policy, agent):
        self.policy, self.agent = policy, agent
        self.original_provider = getattr(agent, 'provider', '')
        self.lock = threading.RLock()
        self.active = True
        self.interrupted = False

    def check(self):
        self.policy.check(self.original_provider, getattr(self.agent, 'provider', ''))

    def poll(self):
        with self.lock:
            if not self.active:
                return False
            try:
                self.check()
            except HeldProvider:
                if not self.interrupted:
                    self.agent.interrupt('Provider held; explicit operator resume required',
                                         hard_cancel=True, propagate_children=False)
                    self.interrupted = True
                return False
        return None


def current_policy():
    # Like fleet ESTOP, this is explicitly installation-wide control, not profile
    # config inheritance. Resolve the canonical native root; never inspect process argv.
    from hermes_constants import get_default_hermes_root, set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config_readonly
    token = set_hermes_home_override(get_default_hermes_root())
    try:
        cfg = load_config_readonly().get('provider_control') or {}
        return Policy(cfg.get('database'), cfg.get('providers', ()))
    finally:
        reset_hermes_home_override(token)


def controlled_turn(fn):
    from functools import wraps
    @wraps(fn)
    def run(agent, *args, **kwargs):
        policy = current_policy()
        if policy.database is None:
            return fn(agent, *args, **kwargs)
        with policy.track(agent):
            return fn(agent, *args, **kwargs)
    return run


def check_request(provider):
    scope = _SCOPE.get()
    if scope is not None:
        scope.check()
        scope.policy.check(provider)
    else:
        current_policy().check(provider)
