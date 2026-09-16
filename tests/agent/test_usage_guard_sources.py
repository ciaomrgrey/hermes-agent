"""Source adapters use native credential resolvers; fixtures contain no credentials."""
import importlib.util


def test_native_read_adapter_is_secret_free(monkeypatch):
    assert importlib.util.find_spec('agent.usage_guard_sources'), 'source adapter missing'
    from agent import usage_guard_sources as source
    monkeypatch.setattr(source.time, 'time', lambda: 1789550000)
    from agent import account_usage as native
    from agent import anthropic_credentials
    monkeypatch.setattr(anthropic_credentials, 'resolve_anthropic_token', lambda: 'synthetic-oauth-secret')
    monkeypatch.setattr(anthropic_credentials, '_is_oauth_token', lambda t: True)
    monkeypatch.setattr(native, '_resolve_codex_usage_credentials', lambda *args: ('synthetic-codex-secret', '', 'synthetic-account'))
    def fetch(url, headers, *, timeout):
        assert timeout <= 15
        if 'anthropic.com' in url:
            return {'seven_day': {'utilization': .5, 'resets_at': '2026-09-18T23:00:00Z'}}
        return {'rate_limit': {'primary_window': {'used_percent': 32, 'limit_window_seconds': 604800, 'reset_at': 1789772400}}}
    monkeypatch.setattr(native, '_get_json', fetch)
    a = source.fetch('anthropic')
    c = source.fetch('openai-codex')
    assert a['pp'] == .5 and c['pp'] == 32
    assert 'secret' not in repr([a, c])
    assert a['grant'] != c['grant']
    assert source.fetch('xai-oauth')['status'] == 'unsupported'
