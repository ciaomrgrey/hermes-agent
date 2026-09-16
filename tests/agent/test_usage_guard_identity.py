"""Known-answer fixtures plus side-effect-free native credential snapshots."""
import hashlib
import json

from agent import usage_guard_sources as source, account_usage as native


def _usage(url, headers, **kwargs):
    if url.endswith('/profile'):
        return {'account':{'uuid':'account-fixture'},'organization':{'uuid':'org-fixture'},'email':'must-not-leak'}
    if 'anthropic' in url:
        return {'seven_day':{'utilization':.5,'resets_at':'2026-09-18T23:00:00Z'}}
    return {'rate_limit':{'primary_window':{'used_percent':39,'limit_window_seconds':604800,'reset_at':1789805425}}}


def test_anthropic_rotation_uses_native_stable_grant_not_token(monkeypatch):
    from agent import anthropic_credentials as anth
    token = ['synthetic-oauth-1']
    monkeypatch.setattr(anth, 'resolve_anthropic_token', lambda: token[0])
    monkeypatch.setattr(anth, '_is_oauth_token', lambda value: True)
    monkeypatch.setattr(native, '_get_json', _usage)
    monkeypatch.setattr(source.time, 'time', lambda:1789550000)
    # New snapshot seam patched once present; old implementation still runs RED.
    monkeypatch.setattr(source, 'credential_snapshots', lambda provider: [(token[0],'',None)], raising=False)
    first = source.fetch('anthropic')
    token[0] = 'synthetic-oauth-2'
    second = source.fetch('anthropic')
    assert first['grant'] == second['grant']
    assert first['account'] == first['grant']
    assert first['identity_kind'] == 'account-organization-fingerprint'
    assert first['pp'] == .5
    assert 'must-not-leak' not in json.dumps([first,second])


def test_multiple_tokens_same_grant_dedupe_but_distinct_grants_refuse(monkeypatch):
    monkeypatch.setattr(source, 'credential_snapshots', lambda provider:[('token-A','',None),('token-B','',None)], raising=False)
    monkeypatch.setattr(source.time, 'time', lambda:1789550000)
    from agent import anthropic_credentials as anth
    monkeypatch.setattr(anth,'_is_oauth_token',lambda value:True)
    identity = ['account-fixture']
    def get(url,headers,**kwargs):
        data = _usage(url,headers,**kwargs)
        if url.endswith('/profile') and headers['Authorization'].endswith('token-B'):
            data['account']['uuid'] = identity[0]
        return data
    monkeypatch.setattr(native,'_get_json',get)
    first = source.fetch('anthropic')
    assert first['status'] == 'ok' and first['pp'] == .5
    identity[0] = 'different-account'
    result = source.fetch('anthropic')
    assert result['status'] == 'unavailable'
    assert result['reason'] == 'multiple subscription grants require separate series'


def test_snapshot_reads_never_call_mutating_resolvers(tmp_path, monkeypatch):
    from agent import anthropic_credentials as anth, credential_pool
    from hermes_cli import auth
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    import agent.secret_scope as scope
    # Native low-level snapshot, with all refresh/heal/select routes made fatal.
    def forbidden(*args,**kwargs):
        raise AssertionError('mutating credential resolver invoked')
    monkeypatch.setattr(anth,'resolve_anthropic_token',forbidden)
    monkeypatch.setattr(credential_pool,'load_pool',forbidden)
    monkeypatch.setattr(native,'_resolve_codex_usage_credentials',forbidden)
    monkeypatch.setattr(auth,'read_credential_pool',lambda p:[{'access_token':'saved-token'}] if p=='openai-codex' else [])
    monkeypatch.setattr(anth,'read_claude_code_credentials',lambda:{'accessToken':'borrowed-token','expiresAt':1})
    monkeypatch.setattr(anth,'read_hermes_oauth_credentials',lambda:None)
    monkeypatch.setattr(scope,'_MULTIPLEX_ACTIVE',True)
    token = set_hermes_home_override(tmp_path)
    secret_token = set_secret_scope({})
    try:
        assert source.credential_snapshots('anthropic') == [('borrowed-token','',None)]
        assert source.credential_snapshots('openai-codex') == [('saved-token','',None)]
    finally:
        reset_secret_scope(secret_token); reset_hermes_home_override(token)
