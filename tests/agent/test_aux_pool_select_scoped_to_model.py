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
    assert aux._resolve_codex_credential_and_base(OTHER)[0] == TOKEN

    # Model A: refused on every path (the mirrored-singleton case is covered below).
    assert aux._select_pool_entry("openai-codex", BENCHED) == (True, None)
    assert aux._resolve_codex_credential_and_base(BENCHED)[0] is None
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
    assert plugin._read_codex_credential()[0] == TOKEN


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


def _write_home(root, *, pool_source="device_code", pool_token=TOKEN, singleton_token=TOKEN, bench=True):
    from agent.credential_pool_model_cooldowns import MODEL_ENTITLEMENT_BENCH_SECONDS
    store = {}
    if pool_source is not None:
        store["credential_pool"] = {"openai-codex": [{
            "id": "d69b79", "label": "chatgpt-lars", "auth_type": "oauth", "priority": 0, "source": pool_source,
            "access_token": pool_token, "refresh_token": "rt-shared", "expires_at_ms": 4_000_000_000_000,
            "model_cooldowns": {BENCHED: time.time() + MODEL_ENTITLEMENT_BENCH_SECONDS} if bench else {},
        }]}
    if singleton_token is not None:
        store["providers"] = {"openai-codex": {"tokens": {"access_token": singleton_token, "refresh_token": "rt-shared"}}}
    (root / "auth.json").write_text(json.dumps(store))


def test_mirrored_singleton_token_does_not_resurrect_the_benched_credential(benched_home):
    """The auth.json singleton IS the benched pool credential: falling back to it must not bypass the bench."""
    import agent.auxiliary_client as aux
    from agent.credential_pool import load_pool

    _write_home(benched_home)
    assert load_pool("openai-codex").select(model=BENCHED) is None  # precondition: pool refuses model A

    assert aux._resolve_codex_credential_and_base(BENCHED)[0] is None
    assert aux.resolve_provider_client("openai-codex", model=BENCHED) == (None, None)
    assert aux.resolve_provider_client("openai-codex", model=BENCHED, raw_codex=True) == (None, None)

    # Model B still recovers with the singleton present.
    assert aux._resolve_codex_credential_and_base(OTHER)[0] == TOKEN
    client, model = aux.resolve_provider_client("openai-codex", model=OTHER)
    assert client is not None and model == OTHER
    raw, _ = aux.resolve_provider_client("openai-codex", model=OTHER, raw_codex=True)
    assert raw is not None and raw.api_key == TOKEN


def test_distinct_singleton_credential_still_serves_the_benched_model(benched_home):
    """A different login in the singleton is a different credential: the bench on the pool row does not apply."""
    import agent.auxiliary_client as aux

    _write_home(benched_home, pool_source="manual", pool_token="tok-other-account", singleton_token=TOKEN)
    assert aux._resolve_codex_credential_and_base(BENCHED)[0] == TOKEN
    raw, _ = aux.resolve_provider_client("openai-codex", model=BENCHED, raw_codex=True)
    assert raw is not None and raw.api_key == TOKEN


def test_no_pool_singleton_fallback_is_kept(benched_home):
    import agent.auxiliary_client as aux

    _write_home(benched_home, pool_source=None)
    assert aux._resolve_codex_credential_and_base(BENCHED)[0] == TOKEN
    raw, _ = aux.resolve_provider_client("openai-codex", model=BENCHED, raw_codex=True)
    assert raw is not None and raw.api_key == TOKEN
