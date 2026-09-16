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
    failure_reason = 'provider_held'


class PersistenceRejected(HeldProvider):
    """A failed native commit cannot become a successful answer."""
    failure_reason = 'session_persistence_failed'


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

    @contextmanager
    def commit_fence(self, *providers):
        """Control DB before session DB; reserve only across the native write.

        A hold writer uses the same SQLite writer exclusion. No distributed
        atomicity is implied: session commit wins before this reservation ends.
        """
        targets = set(providers).intersection(self.providers)
        if not targets or self.database is None:
            yield
            return
        conn = None
        try:
            try:
                conn = sqlite3.connect(self.database.as_uri()+'?mode=rw', uri=True, timeout=1)
                conn.execute('BEGIN IMMEDIATE')
                held = {row[0] for row in conn.execute('SELECT provider FROM holds')}
            except (sqlite3.Error, OSError):
                raise HeldProvider('Provider control state unavailable; commit refused') from None
            if targets.intersection(held):
                raise HeldProvider('Provider held; explicit operator resume required')
            yield
        finally:
            if conn is not None:
                conn.rollback()
                conn.close()

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
        failure_reason = 'provider_held'
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
        except HeldProvider as exc:
            failure_reason = exc.failure_reason
        # Host workers catch Exception, not BaseException. Return the existing
        # interrupted-result protocol at the TURN boundary only; request-level
        # cancellation stays non-retryable and never becomes a fallback result.
        history = kwargs.get('conversation_history', args[1] if len(args)>1 else None) or []
        live_messages = getattr(agent, '_session_messages', None) or []
        if started:
            reject_uncommitted_completion(agent, live_messages)
        messages = list(live_messages) if started else []
        from agent.context_compressor import _DB_PERSISTED_MARKER
        persisted = bool(messages) and all(m.get(_DB_PERSISTED_MARKER) for m in messages)
        if not messages:
            messages = list(history)
            messages.append(dict(role='user', content=kwargs.get('persist_user_message') or user_message))
        if not persisted and getattr(agent, '_session_db', None) is not None:
            # Use native deduplicating flush: held pending input/tool state must
            # survive process exit without manufacturing an assistant completion.
            if not started:
                agent._persist_user_message_idx = len(messages)-1
                agent._persist_user_message_override = None
                agent._persist_user_message_timestamp = kwargs.get('persist_user_timestamp')
                agent._persist_user_message_platform_id = kwargs.get('persist_user_platform_id')
            try:
                agent._persist_session(messages, history)
            except Exception:
                failure_reason = 'session_persistence_failed'
            persisted = bool(messages) and all(m.get(_DB_PERSISTED_MARKER) for m in messages)
        agent._session_messages = messages
        diagnostic = ('Session persistence failed; completion discarded.' if
                      failure_reason == 'session_persistence_failed' else
                      'Provider held; explicit operator resume required.')
        return dict(final_response=diagnostic,
                    interrupted=True, completed=False, failed=True, failure_reason=failure_reason,
                    interrupt_message=None,
                    messages=messages, api_calls=0, agent_persisted=persisted)
    return run


def persistence_guard(agent):
    scope = _SCOPE.get()
    policy = scope.policy if scope is not None else current_policy()
    providers = (
        scope.original_provider if scope is not None else getattr(agent, 'provider', ''),
        getattr(scope.agent if scope is not None else agent, 'provider', ''),
    )
    if policy.database is None or not set(providers).intersection(policy.providers):
        return None
    return lambda: policy.commit_fence(*providers)


def reject_uncommitted_completion(agent, messages):
    from agent.context_compressor import _DB_PERSISTED_MARKER
    # Keep durable history and completed tool rounds; discard only unpublished
    # terminal answers. Never rewrite the already-durable pre-hold prefix.
    messages[:] = [m for m in messages if not (
        m.get('role') == 'assistant' and not m.get('tool_calls')
        and not m.get(_DB_PERSISTED_MARKER))]
    agent._session_messages = messages
    scope = _SCOPE.get()
    if scope is not None:
        scope.agent._session_messages = messages


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
