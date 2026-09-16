"""Read-only grant inventory: native saved stores, no select/refresh/heal.

Whitelist provider fields and hash account/organization IDs. Never print tokens,
emails, account IDs, or whole provider responses. Usage/profile GET only.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_default_hermes_root
from hermes_cli.profiles import list_profile_names, get_profile_dir
from hermes_cli.config import load_config_readonly
from hermes_cli.auth import read_credential_pool
from agent.secret_scope import load_env_file
from agent import anthropic_credentials as anth
from agent.account_usage import _get_json, _codex_headers, _codex_backend_urls
from agent.codex_headers import codex_cloudflare_headers
from agent.usage_guard import parse_weekly


def fingerprint(provider, identity):
    return hashlib.sha256((provider+':'+identity).encode()).hexdigest()


def main():
    out = dict(at=datetime.now(timezone.utc).isoformat(), profiles=[], grants={}, auth_mutations=False)
    # These native credential readers only read file/keychain, never refresh.
    borrowed = anth.read_claude_code_credentials() or {}
    root = get_default_hermes_root()
    home_token = set_hermes_home_override(root)
    try:
        root_anth = anth.read_hermes_oauth_credentials() or {}
    finally:
        reset_hermes_home_override(home_token)
    seen_tokens = {}
    for name in list_profile_names():
        home = get_profile_dir(name)
        context = set_hermes_home_override(home)
        try:
            cfg = load_config_readonly()
            model = cfg.get('model') or {}
            provider = model.get('provider') if isinstance(model, dict) else None
            row = dict(profile=name, provider=provider, candidates=[])
            out['profiles'].append(row)
            if provider not in ('anthropic', 'openai-codex'):
                row['weekly_supported'] = False
                continue
            entries = read_credential_pool(provider)
            secrets = load_env_file(home / '.env')
            candidates = []
            if provider == 'anthropic':
                for key in ('ANTHROPIC_TOKEN','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY'):
                    if secrets.get(key):
                        candidates.append(('profile-env:'+key, secrets[key]))
                local = anth.read_hermes_oauth_credentials() or {}
                for kind, creds in [('claude_code',borrowed),('hermes_pkce',local or root_anth)]:
                    if creds.get('accessToken'):
                        candidates.append((kind, creds['accessToken']))
            for entry in entries:
                token = entry.get('access_token') or entry.get('api_key')
                if token:
                    candidates.append(('pool:'+str(entry.get('source')), token))
                elif provider == 'openai-codex':
                    row.setdefault('unhydrated_pool_sources', []).append(entry.get('source'))
            for kind, token in candidates:
                key = fingerprint(provider, token)
                if key not in seen_tokens:
                    result = dict(status='unavailable')
                    try:
                        if provider == 'anthropic':
                            if not anth._is_oauth_token(token):
                                raise ValueError('non-subscription credential')
                            headers = {'Authorization':'Bearer '+token,'Accept':'application/json',
                                'anthropic-beta':'oauth-2025-04-20','User-Agent':'claude-code/2.1.0'}
                            profile = _get_json('https://api.anthropic.com/api/oauth/profile', headers, timeout=15)
                            account = profile.get('account', {}).get('uuid')
                            org = profile.get('organization', {}).get('uuid')
                            if not isinstance(account,str) or not account or not isinstance(org,str) or not org:
                                raise ValueError('native stable identity unavailable')
                            grant = fingerprint(provider, json.dumps([account,org],separators=(',',':')))
                            raw = _get_json('https://api.anthropic.com/api/oauth/usage', headers, timeout=15)
                            result = dict(parse_weekly(provider,raw), grant=grant,
                                identity_fields=['account.uuid','organization.uuid'])
                        else:
                            account = codex_cloudflare_headers(token).get('ChatGPT-Account-ID')
                            if not account:
                                raise ValueError('native account claim unavailable')
                            grant = fingerprint(provider,account)
                            raw = _get_json(_codex_backend_urls('')[0],_codex_headers(token,account),timeout=15)
                            result = dict(parse_weekly(provider,raw),grant=grant,
                                identity_fields=['https://api.openai.com/auth.chatgpt_account_id'])
                    except Exception as exc:
                        result = dict(status='unavailable',error=type(exc).__name__)
                    seen_tokens[key] = result
                result = seen_tokens[key]
                row['candidates'].append(dict(source=kind, **result))
                if result.get('grant'):
                    grant_record = out['grants'].setdefault(result['grant'],dict(provider=provider, profiles=[]))
                    if name not in grant_record['profiles']:
                        grant_record['profiles'].append(name)
        finally:
            reset_hermes_home_override(context)
    print(json.dumps(out,indent=2))


if __name__ == '__main__':
    main()
