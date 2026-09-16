"""Native quota readers. No network other than subscription usage GETs."""
from __future__ import annotations

import hashlib
import time
from agent.usage_guard import parse_weekly


def fetch(provider):
    if provider not in ('anthropic', 'openai-codex'):
        return parse_weekly(provider, {})
    from agent import account_usage as native
    from agent.anthropic_credentials import _is_oauth_token, resolve_anthropic_token
    try:
        if provider == 'anthropic':
            token = resolve_anthropic_token() or ''
            if not _is_oauth_token(token):
                raise ValueError('OAuth required')
            identity = token
            identity_kind = 'credential-fingerprint; rotation starts new coverage'
            payload = native._get_json('https://api.anthropic.com/api/oauth/usage', {
                'Authorization': 'Bearer '+token, 'Accept': 'application/json',
                'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'claude-code/2.1.0',
            }, timeout=15)
        else:
            token, base, account = native._resolve_codex_usage_credentials(None, None)
            from agent.codex_headers import codex_cloudflare_headers
            claim = codex_cloudflare_headers(token).get('ChatGPT-Account-ID')
            if account and claim and account != claim:
                raise ValueError('conflicting account bindings')
            account = claim or account
            identity = account or token
            identity_kind = 'account-fingerprint' if account else 'credential-fingerprint; rotation starts new coverage'
            payload = native._get_json(native._codex_backend_urls(base)[0], native._codex_headers(token, account), timeout=15)
        result = parse_weekly(provider, payload)
        result['grant'] = hashlib.sha256((provider+':'+identity).encode()).hexdigest()
        result['identity_kind'] = identity_kind
        result['account'] = result['grant'] if identity_kind == 'account-fingerprint' else None
        result['fetched_at'] = time.time()
        result['source'] = 'oauth_usage_api' if provider == 'anthropic' else 'usage_api'
        if result['status'] == 'ok' and result['reset'] <= result['fetched_at']:
            return dict(status='unavailable', reason='stale weekly reset')
        return result
    except Exception as exc:
        return dict(status='unavailable', reason=type(exc).__name__)
