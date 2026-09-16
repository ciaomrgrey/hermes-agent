"""Opt-in provider-scoped admission and durable hold checks.

Not ESTOP: deterministic no-agent jobs are unaffected. Holds are persistent and
never expire. SQLite is shared with the controller; no second control daemon.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class HeldProvider(BaseException):
    """An explicit hold is cancellation, never a retry/fallback candidate."""


_SCOPE: ContextVar[ActiveScope | None] = ContextVar('provider_control_scope', default=None)


class Policy:
    def __init__(self, database, providers):
        self.database = Path(database) if database else None
        self.providers = tuple(providers)

    def check(self, *providers, account=None):
        targets = set(providers).intersection(self.providers)
        if not targets or self.database is None:
            return
        try:
            with sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True, timeout=1) as conn:
                has_account = 'account' in {r[1] for r in conn.execute('PRAGMA table_info(holds)')}
                rows = conn.execute('SELECT provider,account FROM holds' if has_account else
                                    "SELECT provider,NULL FROM holds")
                held = {p for p,a in rows if account is None or a in (None, '*', account)}
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
    def track(self, agent):
        scope = ActiveScope(self, agent)
        scope.check()
        token = _SCOPE.set(scope)
        try:
            yield scope
        finally:
            _SCOPE.reset(token)


class ActiveScope:
    def __init__(self, policy, agent):
        self.policy, self.agent = policy, agent
        self.original_provider = getattr(agent, 'provider', '')

    def check(self):
        self.policy.check(self.original_provider, getattr(self.agent, 'provider', ''))


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
    def run(agent, user_message, *args, **kwargs):
        policy = current_policy()
        if policy.database is None:
            return fn(agent, user_message, *args, **kwargs)
        from agent.auxiliary_client import AuxiliaryExplicitCancellation
        original = getattr(agent, 'provider', '')
        started = False
        try:
            with policy.track(agent) as scope:
                started = True
                result = fn(agent, user_message, *args, **kwargs)
                scope.check()  # owner must not publish a result after the hold edge
                return result
        except AuxiliaryExplicitCancellation:
            # Ordinary user cancellation keeps its existing native semantics.
            try:
                policy.check(original, getattr(agent, 'provider', ''))
            except HeldProvider:
                pass
            else:
                raise
        except HeldProvider:
            pass
        # Host workers catch Exception, not BaseException. Return the existing
        # interrupted-result protocol at the TURN boundary only; request-level
        # cancellation stays non-retryable and never becomes a fallback result.
        history = kwargs.get('conversation_history', args[1] if len(args)>1 else None) or []
        messages = list(getattr(agent, '_session_messages', None) or []) if started else []
        persisted = bool(messages)
        if not messages:
            messages = list(history)
            messages.append(dict(role='user', content=kwargs.get('persist_user_message') or user_message))
        if not started and getattr(agent, '_session_db', None) is not None:
            # Admission precedes the normal turn-start crash persist. Use its
            # native deduplicating flush so a held CLI/Bot Chat input survives
            # process exit; do not manufacture an assistant completion.
            agent._persist_user_message_idx = len(messages)-1
            agent._persist_user_message_override = None
            agent._persist_user_message_timestamp = kwargs.get('persist_user_timestamp')
            agent._persist_user_message_platform_id = kwargs.get('persist_user_platform_id')
            agent._persist_session(messages, history)
            from agent.context_compressor import _DB_PERSISTED_MARKER
            persisted = bool(messages[-1].get(_DB_PERSISTED_MARKER))
        return dict(final_response='Provider held; explicit operator resume required.',
                    interrupted=True, completed=False, failed=True, failure_reason='provider_held',
                    interrupt_message=None,
                    messages=messages, api_calls=0, agent_persisted=persisted)
    return run


def controls_request(policy, provider):
    if policy.database is None:
        return False
    scope = _SCOPE.get()
    return provider in policy.providers or (
        scope is not None and scope.original_provider in scope.policy.providers)


def check_request(provider):
    scope = _SCOPE.get()
    if scope is not None:
        scope.check()
        scope.policy.check(provider)
    else:
        current_policy().check(provider)


def check_profile(profile=None, provider_override=None):
    """Pre-claim scheduler gate; original profile ownership cannot fallback away."""
    policy = current_policy()
    if policy.database is None:
        return
    from hermes_cli.config import load_config_readonly
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(get_profile_dir(profile)) if profile else None
    try:
        model = load_config_readonly().get('model') or {}
        provider = model.get('provider') if isinstance(model, dict) else None
        if not provider or provider == 'auto':
            raise HeldProvider('Unresolved profile provider under enabled control; admission refused')
        policy.check(provider, provider_override)
    finally:
        if token is not None:
            reset_hermes_home_override(token)
