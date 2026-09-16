"""Deterministic native Telegram outbox transport; never an agent turn."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo


def render_notice(body, config):
    item = json.loads(body)
    zone = ZoneInfo(config['timezone'])
    local = lambda value: datetime.fromtimestamp(value, zone).isoformat()
    lines = [f"Weekly quota guard — {item['id']}"]
    if 'window' in item:
        w = item['window']
        lines += [f"{item['provider']}: +{w['pp']:.2f} pp (threshold >{item['threshold_pp']:.2f} pp)",
                  f"{local(w['start'])} → {local(w['end'])}", f"Coverage: {w['coverage']}"]
    else:
        for provider, result in item['providers'].items():
            peak = result.get('peak_pp')
            lines.append(f"{provider}: {'unavailable' if peak is None else f'{peak:.2f} pp'}; {result['coverage']}")
            if peak is not None:
                lines.append(f"{result['start']} → {result['end']}")
        lines.append('Held: '+(', '.join(item['held']) or 'none'))
    lines += [f"Sampling: {item['sampling_seconds']}s; no visibility between observations.",
              'External Chat/Claude.ai clients are not controlled. No remote billing-stop claim.']
    return '\n'.join(lines)


def send_notice(body, config):
    from hermes_cli.send_cmd import _load_hermes_env
    from tools.send_message_tool import send_message_tool
    _load_hermes_env()
    result = json.loads(send_message_tool(dict(action='send', target=config['telegram_target'],
                                               message=render_notice(body,config))))
    if result.get('success') is not True or result.get('skipped') or not result.get('message_id'):
        raise RuntimeError('native Telegram delivery receipt missing')
    return json.dumps(dict(message_id=str(result['message_id'])))
