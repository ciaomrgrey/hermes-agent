"""Subscription GETs using read-only native credential snapshots.

Never load/select/heal a pool or refresh an OAuth token from a diagnostic. The
normal provider auth lifecycle owns refresh. Multiple distinct grants are not
silently merged into one provider time series.
"""
from __future__ import annotations

import hashlib
import json
import time
from agent.usage_guard import parse_weekly


def credential_snapshots(provider):
    from hermes_cli.auth import read_credential_pool
    from agent import anthropic_credentials as anth
    candidates = []
    for entry in read_credential_pool(provider):
        token = entry.get('access_token') or entry.get('api_key')
        if token:
            candidates.append((token, entry.get('base_url') or '', None))
    if provider == 'anthropic':
        env = anth._first_env('ANTHROPIC_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY')
        if env:
            candidates.append((env, '', None))
        for creds in (anth.read_claude_code_credentials(), anth.read_hermes_oauth_credentials()):
            if creds and creds.get('accessToken'):
                candidates.append((creds['accessToken'], '', None))
    if not candidates:
        raise ValueError('no read-only subscription credential snapshot')
    return list(dict.fromkeys(candidates))


def _read(provider, token, base, account):
    from agent import account_usage as native
    if provider == 'anthropic':
        from agent.anthropic_credentials import _is_oauth_token
        if not _is_oauth_token(token):
            raise ValueError('OAuth required')
        headers = {'Authorization':'Bearer '+token, 'Accept':'application/json',
                   'anthropic-beta':'oauth-2025-04-20', 'User-Agent':'claude-code/2.1.0'}
        profile = native._get_json('https://api.anthropic.com/api/oauth/profile', headers, timeout=15)
        account, org = profile.get('account', {}).get('uuid'), profile.get('organization', {}).get('uuid')
        if not isinstance(account,str) or not account or not isinstance(org,str) or not org:
            raise ValueError('stable subscription identity unavailable')
        identity = json.dumps([account,org],separators=(',',':'))
        kind = 'account-organization-fingerprint'
        payload = native._get_json('https://api.anthropic.com/api/oauth/usage', headers, timeout=15)
    else:
        from agent.codex_headers import codex_cloudflare_headers
        claim = codex_cloudflare_headers(token).get('ChatGPT-Account-ID')
        if account and claim and account != claim:
            raise ValueError('conflicting account bindings')
        identity = claim or account
        if not identity:
            raise ValueError('stable subscription identity unavailable')
        kind = 'account-fingerprint'
        payload = native._get_json(native._codex_backend_urls(base)[0], native._codex_headers(token, identity), timeout=15)
    result = parse_weekly(provider,payload)
    result['grant'] = hashlib.sha256((provider+':'+identity).encode()).hexdigest()
    result['account'] = result['grant']
    result['identity_kind'] = kind
    result['fetched_at'] = time.time()
    result['source'] = 'oauth_usage_api' if provider == 'anthropic' else 'usage_api'
    if result['status'] == 'ok' and result['reset'] <= result['fetched_at']:
        return dict(status='unavailable',reason='stale weekly reset')
    return result


def fetch(provider):
    if provider not in ('anthropic','openai-codex'):
        return parse_weekly(provider,{})
    try:
        results = [_read(provider,*snapshot) for snapshot in credential_snapshots(provider)]
        if any(value['status'] != 'ok' for value in results):
            return dict(status='unavailable',reason='incomplete subscription sources')
        if len({value['grant'] for value in results}) != 1:
            return dict(status='unavailable',reason='multiple subscription grants require separate series')
        # Several tokens for the same account are one grant, never summed.
        return results[-1]
    except Exception as exc:
        return dict(status='unavailable',reason=type(exc).__name__)
