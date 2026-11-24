"""A fresh OAuth login must not inherit a terminal entry's display label."""
from dataclasses import replace
from types import SimpleNamespace
import secrets
import time

import pytest

from agent import anthropic_credentials as ac
from agent.credential_pool import load_pool
from hermes_cli import auth_commands as commands
from hermes_cli.auth import write_credential_pool


@pytest.mark.parametrize("explicit_label", [None, "ANTHROPIC-OAUTH-2"])
def test_relogin_retains_dead_entry_with_distinct_label(tmp_path, monkeypatch, explicit_label):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: None)
    write_credential_pool("anthropic", [dict(
        id="571061", label="anthropic-oauth-2", auth_type="oauth", source="manual:hermes_pkce",
        access_token=secrets.token_hex(24), refresh_token=secrets.token_hex(24), priority=0,
        last_status="dead", last_error_reason="invalid_grant", last_status_at=time.time(),
    )])
    login = dict(access_token=secrets.token_hex(24), refresh_token=secrets.token_hex(24),
                 expires_at_ms=int((time.time() + 3600) * 1000))
    spec = commands._OAUTH_ADD_SPECS["anthropic"]
    monkeypatch.setitem(commands._OAUTH_ADD_SPECS, "anthropic", replace(spec, login=lambda args: login))
    pool = load_pool("anthropic")
    fresh = commands._add_credential(SimpleNamespace(label=explicit_label), "anthropic", pool, "oauth")
    persisted = load_pool("anthropic")
    dead = next(e for e in persisted.entries() if e.id == "571061")
    assert fresh.label.casefold() != dead.label.casefold()
    assert dead.last_status == "dead" and dead.last_error_reason == "invalid_grant"
    assert persisted.resolve_target(fresh.label)[1].id == fresh.id
    assert persisted.select().id == fresh.id
