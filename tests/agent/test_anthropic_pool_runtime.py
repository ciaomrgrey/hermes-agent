"""Pooled OAuth must retain model scope through native runtime resolution."""
import json
import time

import pytest

from agent.anthropic_credentials import resolve_anthropic_token
from agent.credential_pool import load_pool
from hermes_cli.auth import AuthError
from hermes_cli.runtime_provider import resolve_runtime_provider

MODEL_A = "claude-fable-5-2"
MODEL_B = "claude-opus-4-6"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))
    for name in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    rows = [dict(id=f"seat{i}", label=f"seat{i}", auth_type="oauth", priority=i,
                 source="manual:hermes_pkce", access_token=f"fixture-access-{i}",
                 refresh_token=f"fixture-refresh-{i}", expires_at_ms=(time.time() + 86400) * 1000)
            for i in range(2)]
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))
    return root


def cool_all():
    pool = load_pool("anthropic")
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=429, model=MODEL_A,
            api_key_hint=entry.access_token, failure_reason="rate_limit",
            error_context={"reset_at": time.time() + 3600})


def test_oauth_model_scope_survives_token_discovery_and_runtime(store):
    cool_all()
    assert resolve_anthropic_token(model=MODEL_B) == "fixture-access-0"
    result = resolve_runtime_provider(requested="anthropic", target_model=MODEL_B)
    assert result["api_key"] == "fixture-access-0"
    assert resolve_anthropic_token(model=MODEL_A) is None
    with pytest.raises(AuthError, match="rate-limited") as caught:
        resolve_runtime_provider(requested="anthropic", target_model=MODEL_A)
    assert caught.value.code == "model_rate_limited"
    assert "No Anthropic credentials" not in str(caught.value)


def test_exhausted_pool_reports_rate_limit_not_sign_out(store):
    cool_all()
    with pytest.raises(AuthError, match="rate-limited"):
        resolve_runtime_provider(requested="anthropic", target_model=MODEL_A)


def test_pool_discovery_skips_only_the_cooled_seat(store):
    pool = load_pool("anthropic")
    pool.mark_exhausted_and_rotate(status_code=429, model=MODEL_A,
        api_key_hint="fixture-access-0", failure_reason="rate_limit")
    assert resolve_anthropic_token(model=MODEL_A) == "fixture-access-1"
    assert resolve_anthropic_token(model=MODEL_B) == "fixture-access-0"


@pytest.mark.parametrize("status,reason,is_rate_limit", [(429, None, True), (401, "auth", False), (402, "billing", False)])
def test_account_cooldown_is_not_confused_with_auth_or_billing(store, status, reason, is_rate_limit):
    pool = load_pool("anthropic")
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=status, api_key_hint=entry.access_token, failure_reason=reason)
    with pytest.raises(AuthError) as caught:
        resolve_runtime_provider(requested="anthropic", target_model=MODEL_A)
    assert (caught.value.code == "rate_limited") is is_rate_limit
    if is_rate_limit:
        assert "rate-limited" in str(caught.value)
        assert "other Claude models remain available" not in str(caught.value)


def test_missing_credentials_still_reports_sign_in(store):
    (store / "auth.json").write_text('{}')
    with pytest.raises(AuthError, match="No Anthropic credentials"):
        resolve_runtime_provider(requested="anthropic", target_model=MODEL_A)
