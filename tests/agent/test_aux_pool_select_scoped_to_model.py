"""Auxiliary pool selection is scoped to the model the aux client will call.

A ChatGPT-account entitlement 400 benches one (credential, model) pair for a year. The auxiliary
Codex path used to select from the pool with no model, and a model-less select treats ANY active
model cooldown as blocking — so one bench on model A hid the shared credential from every aux
Codex call (completion gate, compression, titles) for every model, while main turns (which pass
their model) kept working. Each aux path now passes the model it will actually call.

Runs against a throwaway HERMES_HOME seeded with a real pool row; no live auth.json is touched.
"""
import json
import time

import pytest

BENCHED = "gpt-5.5"
OTHER = "gpt-6-astra"
TOKEN = "tok-shared-chatgpt"


@pytest.fixture
def benched_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    for var in ("OPENAI_API_KEY", "HERMES_CODEX_BASE_URL", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
    from agent.credential_pool_model_cooldowns import MODEL_ENTITLEMENT_BENCH_SECONDS
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"openai-codex": [{
        "id": "d69b79", "label": "chatgpt-lars", "auth_type": "oauth", "priority": 0, "source": "manual",
        "access_token": TOKEN, "refresh_token": "rt-shared", "expires_at_ms": 4_000_000_000_000,
        "model_cooldowns": {BENCHED: time.time() + MODEL_ENTITLEMENT_BENCH_SECONDS},
    }]}}))
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "_client_cache", {})
    return root


def test_entitlement_bench_on_one_model_does_not_block_aux_codex_for_another(benched_home):
    import agent.auxiliary_client as aux

    # Precondition: the pool really carries the bench, and an unscoped select is still blocked
    # (the conservative rule stays for callers that genuinely do not know their model).
    from agent.credential_pool import load_pool
    assert load_pool("openai-codex").select() is None

    # Model B: wrapped aux client, raw Responses client and the token reader all resolve.
    client, model = aux.resolve_provider_client("openai-codex", model=OTHER)
    assert type(client).__name__ == "CodexAuxiliaryClient" and model == OTHER
    raw, raw_model = aux.resolve_provider_client("openai-codex", model=OTHER, raw_codex=True)
    assert raw is not None and raw.api_key == TOKEN and raw_model == OTHER
    assert aux._read_codex_access_token(OTHER) == TOKEN

    # Model A: refused on every path (no singleton token exists in this throwaway home).
    assert aux._select_pool_entry("openai-codex", BENCHED) == (True, None)
    assert aux._read_codex_access_token(BENCHED) is None
    assert aux.resolve_provider_client("openai-codex", model=BENCHED) == (None, None)
    assert aux.resolve_provider_client("openai-codex", model=BENCHED, raw_codex=True) == (None, None)


def test_codex_image_plugin_is_not_hidden_by_a_chat_model_bench(benched_home):
    import importlib.util
    from pathlib import Path

    import agent.auxiliary_client as aux
    plugin_init = Path(aux.__file__).resolve().parents[1] / "plugins/image_gen/openai-codex/__init__.py"
    spec = importlib.util.spec_from_file_location("_codex_image_plugin_under_test", plugin_init)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    assert plugin._read_codex_access_token() == TOKEN


def test_anthropic_aux_selects_for_its_own_model(tmp_path, monkeypatch):
    """Sibling path: a per-model 429 on another Claude model must not bench the aux model."""
    import agent.anthropic_credentials as anth_cred
    import agent.auxiliary_client as aux
    from hermes_cli.auth import write_credential_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(anth_cred, "read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(anth_cred, "resolve_anthropic_token", lambda: None)
    aux_model = aux._get_aux_model_for_provider("anthropic") or "claude-haiku-4-5-20251001"
    write_credential_pool("anthropic", [{
        "id": "a1", "label": "pooled", "auth_type": "api_key", "priority": 0, "source": "manual",
        "access_token": "sk-ant-api03-pooled", "model_cooldowns": {"claude-opus-4-1": time.time() + 600},
    }])
    client, model = aux._try_anthropic()
    assert client is not None and model == aux_model

    write_credential_pool("anthropic", [{
        "id": "a1", "label": "pooled", "auth_type": "api_key", "priority": 0, "source": "manual",
        "access_token": "sk-ant-api03-pooled", "model_cooldowns": {aux_model: time.time() + 600},
    }])
    assert aux._try_anthropic() == (None, None)
