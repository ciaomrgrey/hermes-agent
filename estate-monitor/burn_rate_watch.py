"""Notification-only sensor for native Ripley cron 5291b75fe0f1.

No LLM, source DB writes, account mutations, or manual message sends.
Only measurement/checkpoint files are written; native cron owns delivery.
"""
import math
import re
from datetime import datetime, timezone


def percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('invalid percentage type')
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('invalid percentage range')
    return float(value)


def parse_anthropic(payload):
    windows = {}
    for key, value in payload.items():
        if key == 'extra_usage' or not isinstance(value, dict) or value.get('utilization') is None:
            continue
        reset = datetime.fromisoformat(value['resets_at'].replace('Z', '+00:00')) if value.get('resets_at') else None
        if reset is not None and reset.tzinfo is None:
            raise ValueError('quota reset lacks timezone')
        windows['anthropic/' + key] = {'used': percentage(value['utilization']), 'reset': reset.timestamp() if reset else None}
    for limit in payload.get('limits') or []:
        if not isinstance(limit, dict) or not isinstance(limit.get('kind'), str):
            raise ValueError('invalid dynamic quota limit')
        kind = limit['kind']
        if ((kind == 'session' and 'anthropic/five_hour' in windows)
                or (kind == 'weekly_all' and 'anthropic/seven_day' in windows)):
            continue
        scope = limit.get('scope') or {}
        scope_parts = []
        for scope_kind, scope_value in sorted(scope.items()):
            if scope_value is None:
                continue
            if isinstance(scope_value, dict):
                identifier = scope_value.get('id') or scope_value.get('display_name')
            else:
                identifier = scope_value
            if identifier is not None:
                scope_parts.append(f'{scope_kind}/{identifier}')
        name = '/'.join(['anthropic', kind] + scope_parts)
        reset = datetime.fromisoformat(limit['resets_at'].replace('Z', '+00:00')) if limit.get('resets_at') else None
        if reset is not None and reset.tzinfo is None:
            raise ValueError('quota reset lacks timezone')
        windows[name] = {
            'used': percentage(limit['percent']),
            'reset': reset.timestamp() if reset else None,
        }
    # utilization is already percentage points (0.5 means 0.5%, NOT 50%).
    extra = payload.get('extra_usage') or {}
    return windows, {k: extra[k] for k in ('is_enabled', 'used_credits', 'monthly_limit', 'currency') if k in extra}


def new_alerts(findings, previous):
    return [value['text'] for value in new_findings(findings, previous)]


def new_findings(findings, previous):
    changed = [value for key, value in findings.items()
               if key not in previous or previous[key]['level'] != value['level']
               or previous[key].get('episode') != value.get('episode')
               or previous[key].get('milestone') != value.get('milestone')]
    priorities = {'critical': 0, 'unmeasured': 2, 'warning': 3}
    return sorted(changed, key=lambda value: (
        1 if value.get('action') and value['level'] != 'critical'
        else priorities.get(value['level'], 9),
        value['text']))

def evaluate(current, history, bind_actions=False):
    findings = {}
    for name, window in sorted(current['quota'].items()):
        used = window['used']
        threshold = 20 if name == 'anthropic/five_hour' else 70
        if used >= threshold:
            milestone = 100 if used >= 100 else 99 if used >= 99 else 90 if used >= 90 else threshold
            findings['quota:' + name] = {
                'level': 'critical' if used >= 90 else 'warning',
                'episode': round(window['reset']/60) if window.get('reset') else None,
                'milestone': milestone,
                'text': f'⚠️ {name}: {used:g}% used (trigger {threshold}%).',
            }
        for past in history:
            age = current['at'] - past['at']
            old = past['quota'].get(name)
            if not old or not (0 < age <= 18000):
                continue
            if window.get('reset') is None or old.get('reset') is None:
                continue
            if abs(window['reset'] - old['reset']) > 60:
                continue
            delta = used - old['used']
            if (age <= 3600 and delta >= 10) or delta >= 20:
                findings['burst:' + name] = {
                    'level': 'warning',
                    'episode': round(window['reset']/60),
                    'text': f'⚠️ {name} burst: +{delta:g}pp/{age/60:.0f}m (trigger +10pp/1h).',
                }
                break
    for hours, threshold in ((1, 25), (12, 200)):
        start = current['at'] - hours * 3600
        candidates = [s for s in history if start <= s['at'] < current['at']]
        baseline = min(candidates, key=lambda s: s['at']) if candidates else None
        totals, source_jobs, unbound = {}, {}, set()
        for key, row in current['rows'].items():
            group = row['profile'] + '/' + row['provider']
            # Entire row is inside this window: a real historical lower bound.
            count = row['calls'] if start <= row['first'] <= row['last'] <= current['at'] else 0
            old = baseline['rows'].get(key) if baseline else None
            if old and (row['first'] != old['first'] or row['calls'] < old['calls']):
                findings['counter:' + group] = {'level': 'unmeasured', 'text': f'UNMEASURED — {group} ledger counter reset; no negative or invented delta.'}
            elif old:
                count = max(count, row['calls'] - old['calls'])
            elif baseline and baseline['at'] <= row['first'] <= row['last'] <= current['at']:
                count = max(count, row['calls'])
            totals[group] = totals.get(group, 0) + count
            if count:
                job_id = row.get('source_job_id')
                job_name = row.get('source_job_name')
                if job_id and job_name:
                    source_jobs.setdefault(group, {})[job_id] = job_name
                else:
                    unbound.add(group)
        if baseline:
            for key, old in baseline['rows'].items():
                if key in current['rows'] or old['last'] < start:
                    continue
                group = old['profile'] + '/' + old['provider']
                findings.setdefault('counter:' + group, {
                    'level': 'unmeasured',
                    'text': f'UNMEASURED — {group} ledger row disappeared; delta unavailable.',
                })
        for group, calls in sorted(totals.items()):
            if calls >= threshold:
                finding = {
                    'level': 'warning',
                    'text': f'⚠️ {group}: ≥{calls} calls/{hours}h (trigger {threshold}).',
                }
                jobs = source_jobs.get(group, {})
                profile = group.split('/', 1)[0]
                if (
                    bind_actions and profile == 'gurney'
                    and group not in unbound and len(jobs) == 1
                ):
                    job_id, job_name = next(iter(jobs.items()))
                    profile = group.split('/', 1)[0]
                    finding['text'] = (
                        f'⚠️ Pause {profile}/{job_name}: ≥{calls} calls/{hours}h '
                        f'(trigger {threshold}).')
                    finding['action'] = {
                        'kind': 'cron.pause', 'profile': profile,
                        'job_id': job_id, 'expected_name': job_name,
                    }
                findings[f'calls:{group}:{hours}h'] = finding
    for error in current['errors']:
        findings['source:' + error] = {'level': 'unmeasured', 'text': 'UNMEASURED — ' + error + '.'}
    return findings


PROFILES = ('generalist', 'cody', 'emma', 'sophia', 'jared', 'plutus', 'ripley', 'gurney')
JOB_ID = '5291b75fe0f1'


def read_ledger(root, profiles=PROFILES):
    import hashlib
    import json
    import sqlite3
    rows, errors, counts = {}, [], {}
    for profile in profiles:
        try:
            path = root / profile / 'state.db'
            job_names = {}
            jobs_path = root / profile / 'cron/jobs.json'
            if jobs_path.exists():
                try:
                    job_names = {j['id']: j['name'] for j in json.loads(jobs_path.read_text())['jobs']}
                except (ValueError, KeyError, TypeError):
                    job_names = {}
            con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
            try:
                con.row_factory = sqlite3.Row
                records = con.execute('SELECT session_id,model,billing_provider,billing_base_url,billing_mode,task,api_call_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,first_seen,last_seen FROM session_model_usage').fetchall()
            finally:
                con.close()
            pending = {}
            for record in records:
                r = dict(record)
                identity = [profile] + [r[k] for k in ('session_id', 'model', 'billing_provider', 'billing_base_url', 'billing_mode', 'task')]
                key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                first, last = float(r['first_seen']), float(r['last_seen'])
                calls = r['api_call_count']
                if not isinstance(calls, int) or calls < 0 or not math.isfinite(first + last) or first > last:
                    raise ValueError('malformed usage counters')
                session_id = str(r['session_id'] or '')
                source_match = re.match(r'^cron_([a-f0-9]{12})(?:_|$)', session_id)
                source_job_id = source_match.group(1) if source_match else None
                pending[key] = {'profile': profile, 'provider': r['billing_provider'] or 'unknown',
                                'billing_mode': r['billing_mode'] or '',
                                'model': r['model'], 'calls': calls, 'first': first, 'last': last,
                                'tokens': {k: r[k] for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens')}}
                if source_job_id in job_names:
                    pending[key]['source_job_id'] = source_job_id
                    pending[key]['source_job_name'] = job_names[source_job_id]
            rows.update(pending)
            counts[profile] = len(pending)
        except Exception as exc:
            errors.append(f'{profile}/state.db session_model_usage: {type(exc).__name__}')
    return rows, errors, counts


def atomic_json(path, value):
    import json
    import os
    import tempfile
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def accounting_status(rows, quota):
    """Describe only providers that ledgers prove the estate actually used."""
    providers = {}
    for row in rows.values():
        provider = row.get('provider') or 'unknown'
        providers.setdefault(provider, set()).add(row.get('billing_mode') or '')
    status, errors = {}, []
    for provider, modes in sorted(providers.items()):
        quota_measured = any(name.startswith(provider + '/') for name in quota)
        entry = {
            'calls_and_tokens': 'measured',
            'quota': 'measured' if quota_measured else 'unmeasured',
            'billing': 'subscription_included' if modes == {'subscription_included'} else 'unmeasured',
            'billed_usage': 'unmeasured',
            'billing_modes_seen': sorted(mode or 'unknown' for mode in modes),
        }
        if entry['quota'] == 'unmeasured':
            errors.append(f'{provider} quota: no live account quota reader')
        if any(mode not in ('', 'subscription_included') for mode in modes):
            errors.append(f'{provider} billed usage: no live billing reader')
        status[provider] = entry
    return status, errors


def run_once(current, directory, retry_delivery=False, approval_controls=False):
    import copy
    import json
    current = copy.deepcopy(current)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'state.json'
    state = {'version': 1, 'history': [], 'findings': {}}
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            if saved['version'] != 1 or not isinstance(saved['history'], list) or not isinstance(saved['findings'], dict):
                raise ValueError('checkpoint schema')
            state = saved
        except (ValueError, KeyError, TypeError):
            current['errors'].append('rate checkpoint corrupt: delta unavailable; absolute/history lower-bound checks still active')
    history = [s for s in state['history'] if 0 < current['at']-s['at'] <= 43200]
    if state['history'] and current['at'] <= state['history'][-1]['at']:
        current['errors'].append('observation clock moved backwards: rate history discarded')
    findings = evaluate(current, history, bind_actions=approval_controls)
    previous = state['findings']
    changed = new_findings(findings, previous)
    stamp = datetime.fromtimestamp(current['at'], timezone.utc).isoformat(timespec='seconds')
    # Pending notices retain their ORIGINAL observation timestamp until native
    # cron acknowledges delivery, even if the condition has since cleared.
    saved_pending = state.get('pending', {})
    # Migrate the v1 text->timestamp checkpoint shape without nesting notices.
    pending = {
        key: (value if isinstance(value, dict) else {'text': key, 'observed': value})
        for key, value in saved_pending.items()
    }
    # A successful cron completion acknowledges only the one standalone issue
    # selected on the previous run.  Delivery failures retain the full queue.
    if pending and not retry_delivery:
        pending.pop(next(iter(pending)))
    for finding in changed:
        pending.setdefault(finding['text'], {
            'text': finding['text'], 'observed': stamp,
            **({'action': finding['action']} if finding.get('action') else {}),
        })
    # The live job's stricter attention contract is one standalone issue.
    selected = list(pending.values())[:1]
    if selected and all(item.get('action') for item in selected):
        output = json.dumps({
            'hermes_cron_approval': 1,
            'recommendations': [
                {'text': item['text'], 'button': 'Approve pause', 'action': item['action']}
                for item in selected
            ],
        }, separators=(',', ':'), allow_nan=False)
    else:
        output = '\n'.join(item['text'] for item in selected)
    if not output:
        output = '✅ Nothing to report.'
    latest = {'version': 1, 'sample': current, 'findings': findings, 'output': output,
              'limitations': 'Notification-only. API-call lower bounds include only contained rows or real cumulative-counter differences; no exact historical rate for overlapping rows without a baseline. Token categories kept separate, never converted to money/quota. “Nothing to report” means no NEW alert, never a global all-clear.'}
    atomic_json(directory / 'latest.json', latest)
    # Keep 12h of bounded checkpoints, not prior model prose.
    history.append({k: current[k] for k in ('at', 'quota', 'rows', 'errors')})
    for sample in history:
        for row in sample['rows'].values():
            row.pop('tokens', None)
    atomic_json(path, {'version': 1, 'history': history[-64:], 'findings': findings, 'pending': pending, 'pending_at': current['at'] if pending else None})
    with (directory / 'observations.jsonl').open('a') as log:
        log.write(json.dumps({'at': current['at'], 'quota': current['quota'], 'profile_counts': current.get('profile_counts', {}), 'errors': current['errors'], 'findings': findings, 'emitted': bool(output)}, allow_nan=False) + '\n')
    return output


def anthropic_usage():
    import httpx
    from agent.anthropic_credentials import resolve_anthropic_token, _is_oauth_token
    token = resolve_anthropic_token()
    if not token or not _is_oauth_token(token):
        raise ValueError('OAuth usage credential unavailable')
    with httpx.Client(timeout=15) as client:
        response = client.get('https://api.anthropic.com/api/oauth/usage', headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
            'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'claude-code/2.1.0'})
        response.raise_for_status()
        return response.json()


def codex_usage():
    from agent.account_usage import fetch_account_usage
    return fetch_account_usage('openai-codex')


def collect(root, anthropic_reader=anthropic_usage, codex_reader=codex_usage):
    import time
    sample = {'at': time.time(), 'quota': {}, 'rows': {}, 'errors': [], 'sources': {}}
    try:
        windows, extra = parse_anthropic(anthropic_reader())
        if not windows or any(v['reset'] is not None and v['reset'] <= time.time() for v in windows.values()):
            raise ValueError('missing or expired quota windows')
        sample['quota'].update(windows)
        sample['anthropic_extra_usage'] = extra
        sample['sources']['anthropic'] = {'at': time.time(), 'source': 'https://api.anthropic.com/api/oauth/usage'}
        for name, value in windows.items():
            if value['reset'] is None:
                sample['errors'].append(name + ': reset absent; absolute percentage measured, reset-aware rate unavailable')
        for required in ('five_hour', 'seven_day'):
            if 'anthropic/' + required not in windows:
                sample['errors'].append('Anthropic OAuth quota ' + required + ': window absent')
        if extra.get('is_enabled') is True:
            sample['errors'].append('Anthropic extra usage enabled: additional-charge exposure, provider values retained without assuming currency units')
    except Exception as exc:
        sample['errors'].append('Anthropic OAuth quota: ' + type(exc).__name__)
    try:
        snapshot = codex_reader()
        if not snapshot or not snapshot.available or not snapshot.windows:
            raise ValueError('no live Codex quota snapshot')
        if not 0 <= time.time() - snapshot.fetched_at.timestamp() <= 180:
            raise ValueError('stale Codex snapshot')
        windows = {}
        for window in snapshot.windows:
            if not window.reset_at or window.reset_at.timestamp() <= time.time():
                raise ValueError('absent or expired Codex reset')
            windows['openai-codex/' + window.label.lower().replace(' ', '_')] = {
                'used': percentage(window.used_percent), 'reset': window.reset_at.timestamp()}
        sample['quota'].update(windows)
        sample['sources']['openai-codex'] = {'at': snapshot.fetched_at.timestamp(), 'source': 'native agent.account_usage usage_api'}
    except Exception as exc:
        sample['errors'].append('Codex OAuth quota: ' + type(exc).__name__)
    profiles = sorted(set(PROFILES) | {p.parent.name for p in root.glob('*/state.db')})
    sample['rows'], errors, sample['profile_counts'] = read_ledger(root, profiles)
    sample['errors'].extend(errors)
    sample['provider_accounting'], accounting_errors = accounting_status(
        sample['rows'], sample['quota'])
    sample['errors'].extend(accounting_errors)
    sample['at'] = time.time()
    return sample


def main():
    import argparse
    import fcntl
    import json
    import os
    import sys
    from pathlib import Path
    # This deterministic sensor intentionally runs on macOS' system Python 3.9.
    # Avoid importing the Hermes runtime (which requires a newer interpreter).
    home = Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=home/'cron/burn-rate-watch')
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir/'sensor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retry_delivery = False
        jobs = home/'cron/jobs.json'
        job = {}
        if jobs.exists():
            job = next((j for j in json.loads(jobs.read_text())['jobs'] if j['id'] == JOB_ID), {})
            checkpoint = args.state_dir/'state.json'
            try:
                saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
                if saved.get('pending'):
                    completed = datetime.fromisoformat(job['last_run_at']).timestamp() if job.get('last_run_at') else 0
                    acknowledged = job.get('last_status') == 'ok' and not job.get('last_delivery_error') and completed >= saved.get('pending_at', float('inf'))
                    retry_delivery = not acknowledged
            except (ValueError, TypeError, KeyError):
                retry_delivery = True
        output = run_once(
            collect(home.parent), args.state_dir,
            retry_delivery=retry_delivery,
            approval_controls=job.get('approval_controls') is True,
        )
        if output:
            print(output)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # A failed sensor is always loud, never silent success; no secret-bearing exception strings.
        print('UNMEASURED — burn-rate sensor failed: ' + type(exc).__name__)
        raise SystemExit(1)
