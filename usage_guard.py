#!/usr/bin/env python3
"""Local candidate CLI. Activation is refused until full native control qualifies."""
import argparse
import fcntl
import json
import math
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.usage_guard import Guard
from agent.usage_guard_sources import fetch

RELEASE_BLOCKER = 'request-owned auxiliary cancellation and all admission integrations are not qualified'


def load_config(path):
    cfg = json.loads(path.read_text())
    for key in ('threshold_pp', 'window_seconds', 'sample_seconds', 'retention_days'):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError('invalid '+key)
    if cfg['retention_days']*86400 < 86400+cfg['window_seconds']:
        raise ValueError('retention must cover report plus window lookback')
    if cfg['telegram_target'] != 'telegram:471605389':
        raise ValueError('Telegram target does not match owner contract')
    ZoneInfo(cfg['timezone'])
    for key in ('night_start', 'night_end', 'report_time'):
        datetime.strptime(cfg[key], '%H:%M')
    if not isinstance(cfg.get('delivery_enabled', False), bool):
        raise ValueError('invalid delivery_enabled')
    if cfg.get('armed'):
        raise ValueError(RELEASE_BLOCKER)
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('sample', 'report', 'status'))
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    args = parser.parse_args()
    config_path = args.config.resolve()
    try:
        cfg = load_config(config_path)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')
    # One bounded invocation; native cron owns scheduling. Cross-process lock
    # covers read/sample/outbox phases; no second timer or hidden worker loop.
    with config_path.with_name('usage_guard.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(3, 'another guard invocation owns the lock\n')
        guard = Guard(config_path.with_name('state.sqlite3'), cfg)
        try:
            now = time.time()
            if args.command == 'sample':
                result = {}
                for provider in ('anthropic', 'openai-codex', 'xai-oauth'):
                    value = fetch(provider)
                    at = value.get('fetched_at', time.time())
                    result[provider] = dict(source=value, rolling=guard.sample(provider, at, value))
                local = datetime.fromtimestamp(now, ZoneInfo(cfg['timezone']))
                if local.strftime('%H:%M') >= cfg['report_time']:
                    guard.report(now)
            elif args.command == 'report':
                result = guard.report(now)
            else:
                result = dict(armed=False, release_ready=False, release_blocker=RELEASE_BLOCKER,
                              held=guard.holds(), pending_alerts=len(guard.outbox()),
                              storage=guard.storage_status(),
                              sampling_seconds=cfg['sample_seconds'], no_live_controls_installed=True)
            if args.command != 'status' and cfg.get('delivery_enabled', False):
                from agent.usage_guard_delivery import send_notice
                guard.deliver(lambda body: send_notice(body, cfg))
            if args.command != 'status':
                guard.maintain(now)
            print(json.dumps(result, indent=2))
        finally:
            guard.close()


if __name__ == '__main__':
    main()
