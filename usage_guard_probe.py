"""Read-only subscription probe. No inference, alerts or pause side effects."""
import json
from datetime import datetime, timezone
from agent.usage_guard_sources import fetch
from agent.transports.codex_app_server import CodexAppServerClient

out = {'at': datetime.now(timezone.utc).isoformat(), 'sources': {p: fetch(p) for p in ('anthropic', 'openai-codex', 'xai-oauth')}}
client = None
try:
    client = CodexAppServerClient()
    client.initialize(timeout=10)
    raw = client.request('account/rateLimits/read', timeout=15)
    # Keep only percentage/duration/reset scalars; names alone do not define weekly.
    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k,v in value.items() if isinstance(v, (dict, list)) or k in ('usedPercent', 'windowDurationMins', 'resetsAt')}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    out['app_server'] = scrub(raw)
except Exception as exc:
    out['app_server_error'] = type(exc).__name__
finally:
    if client:
        client.close()
        out['app_server_closed'] = not client.is_alive()
print(json.dumps(out, indent=2))
