"""Independent recommendation-only gate check; run even when the plugin cannot load.

Install outside the plugin package and invoke with the installed Hermes interpreter.
Never modifies config, gate state or core. stdout is bounded, content-free JSON.
"""
import argparse
from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def send_alarm(event):
    import subprocess
    import sys
    from hermes_cli.profiles import get_profile_dir
    from tools.environments.local import served_profile_child_env
    try:
        receipt = send_bot_alarm(event)
    except Exception:
        receipt = {'status': 'unavailable'}
    if receipt.get('status') not in {'unavailable', 'unverified'}:
        return receipt
    text = 'Completion-gate alarm: profile={} reasons={}'.format(
        event.get('profile', event.get('profile_home_sha256', 'unknown')),
        ','.join(event['reasons']))
    text = ' '.join(text.splitlines())
    try:
        env = served_profile_child_env(target_home=get_profile_dir('generalist'), inherit_credentials=True)
        proc = subprocess.run([sys.executable, '-m', 'hermes_cli.main', '-p', 'generalist',
            'send', '-t', 'telegram:471605389', '--json', '-f', '-'],
            input=text, text=True, capture_output=True, timeout=30, env=env)
        result = json.loads(proc.stdout)
        sent = proc.returncode == 0 and result.get('success') is True and not result.get('skipped')
        return {'status': 'sent' if sent else 'unavailable', 'transport': 'telegram',
                'bot_status': receipt['status'], 'receipt': result}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'status': 'unavailable', 'transport': 'telegram', 'bot_status': receipt['status']}


def send_bot_alarm(event):
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_live_delivery import find_canonical_live_owner, deliver_to_live_owner, read_delivery_result
    home = get_profile_dir('gurney')
    text = ('Completion-gate health alarm (recommendation only; no deployment permission).\n'
            + json.dumps(event, sort_keys=True)
            + '\nInspect the external gate check and native activity. Unavailable delivery falls back to the authorized Telegram alarm.')
    key = hashlib.sha256((str(home.resolve()) + text).encode()).hexdigest()
    receipt = read_delivery_result(home, key)
    if receipt is None:
        owner = find_canonical_live_owner(home)
        if owner is None:
            return {'status': 'unavailable', 'delivery_id': key}
        deliver_to_live_owner(home, owner, text, delivery_id=key,
                              author={'kind': 'plugin', 'name': 'completion-gate-health'})
        receipt = read_delivery_result(home, key)
    if receipt is None or receipt.get('message') != text:
        return {'status': 'unverified', 'delivery_id': key}
    return {'status': receipt['status'], 'delivery_id': key}


@contextmanager
def state_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS authorizations (home TEXT PRIMARY KEY, digest TEXT, reference_sha256 TEXT, expires REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS alarms (home TEXT PRIMARY KEY, reasons TEXT, opened REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS confirmed_alarms (home TEXT PRIMARY KEY, reasons TEXT, opened REAL)')
        yield db


def config_state(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config_effective import load_user_config_effective
    token = set_hermes_home_override(Path(home).resolve())
    try:
        cfg = load_user_config_effective(Path(home) / 'config.yaml', fail_closed=True)
    finally:
        reset_hermes_home_override(token)
    plugins = cfg.get('plugins', {})
    entry = plugins.get('entries', {}).get('completion-gate', {})
    enabled = ('completion-gate' in plugins.get('enabled', [])
               and 'completion-gate' not in plugins.get('disabled', [])
               and entry.get('enabled', True) is not False
               and entry.get('settings', {}).get('enabled', False) is True)
    digest = hashlib.sha256(json.dumps(plugins, sort_keys=True).encode()).hexdigest()
    return enabled, digest, entry.get('settings', {})


def authorize_disable(home, state_path, *, reference, expires):
    enabled, digest, _ = config_state(home)
    if enabled or not reference.strip():
        raise ValueError('authorization_requires_disabled_config_and_reference')
    with state_db(state_path) as db:
        db.execute('INSERT OR REPLACE INTO authorizations VALUES(?,?,?,?)',
                   (str(Path(home).resolve()), digest, hashlib.sha256(reference.encode()).hexdigest(), expires))


def activity_gap(home, gate_path, state_path, *, now, tolerance):
    """Compare persisted text finals to this profile's gate activity, not wall time.

    A 60s settling delay excludes in-flight writes. Advance only healthy intervals;
    missing events remain visible during later quiet periods. Persistence is turn
    activity, not proof of a messaging transport receipt.
    """
    from hermes_constants import profile_name_for_home
    state = Path(home) / 'state.db'
    if not state.exists():
        return False
    key = str(Path(home).resolve())
    with state_db(state_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS cursors (home TEXT PRIMARY KEY, through REAL)')
        row = db.execute('SELECT through FROM cursors WHERE home=?', (key,)).fetchone()
        start = row[0] if row else now - 600
        end = now - 60
        with closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True)) as activity:
            finals = activity.execute("SELECT timestamp FROM messages WHERE role='assistant' "
                "AND content IS NOT NULL AND content != '' AND (tool_calls IS NULL OR tool_calls='[]') "
                "AND coalesce(display_kind,'') != 'delivery_mirror' "
                "AND timestamp > ? AND timestamp <= ? ORDER BY timestamp LIMIT 1000", (start, end)).fetchall()
        if len(finals) == 1000:
            end = finals[-1][0]
        gap = bool(finals)
        gate_path = Path(gate_path).resolve()
        if finals and gate_path.exists():
            with closing(sqlite3.connect(gate_path.as_uri() + '?mode=ro', uri=True)) as gate:
                profile = profile_name_for_home(home) or 'default'
                gap = any(not gate.execute('SELECT 1 FROM events WHERE profile=? AND created BETWEEN ? AND ? LIMIT 1',
                    (profile, ts - tolerance, ts + tolerance)).fetchone() for (ts,) in finals)
        if not gap:
            db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (key, end))
        return gap


def check(home, state_path, *, now=None, send=send_alarm):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.plugins import PluginManager, VALID_HOOKS
    now = float(time.time() if now is None else now)
    home = Path(home).resolve()
    enabled, digest, settings = config_state(home)
    reasons = []
    with state_db(state_path) as db:
        authorization = db.execute('SELECT digest, expires FROM authorizations WHERE home=?', (str(home),)).fetchone()
    if not enabled and not (authorization and authorization[0] == digest and now < authorization[1]):
        reasons.append('unauthorized_disable')
    token = set_hermes_home_override(home)
    try:
        if enabled:
            if 'before_turn_end' not in VALID_HOOKS:
                reasons.append('core_hook_missing')
            manager = PluginManager()
            manager.discover_and_load()
            loaded = manager._plugins.get('completion-gate')
            if not loaded or not loaded.enabled or loaded.module is None:
                reasons.append('package_unavailable')
            elif not manager.has_hook('before_turn_end'):
                reasons.append('hook_unregistered')
            from hermes_constants import get_default_hermes_root
            gate_path = settings.get('db_path', get_default_hermes_root() / 'state/completion-gate/gate.db')
            if activity_gap(home, gate_path, state_path, now=now,
                            tolerance=float(settings.get('total_timeout', 20)) + 60):
                reasons.append('liveness_gap')
    finally:
        reset_hermes_home_override(token)
    result = {'ok': not reasons, 'reasons': reasons}
    if reasons:
        state_path = Path(state_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with state_db(state_path) as db:
            key = json.dumps(reasons)
            previous = db.execute('SELECT reasons, opened FROM alarms WHERE home=?', (str(home),)).fetchone()
            opened = previous[1] if previous and previous[0] == key else now
            db.execute('INSERT OR REPLACE INTO alarms VALUES(?,?,?)', (str(home), key, opened))
        event = {'target': 'gurney', 'profile': home.name if home.parent.name == 'profiles' else 'default',
                 'profile_home_sha256': hashlib.sha256(str(home).encode()).hexdigest(),
                 'reasons': reasons, 'opened': opened}
        try:
            with state_db(state_path) as db:
                confirmed = db.execute('SELECT reasons, opened FROM confirmed_alarms WHERE home=?', (str(home),)).fetchone()
            if confirmed == (key, opened):
                result['delivery'] = {'status': 'already_sent'}
            else:
                result['delivery'] = send(event)
                # A queued/unverified receipt is not confirmed transport delivery.
                if result['delivery'].get('status') in {'sent', 'delivered'}:
                    with state_db(state_path) as db:
                        db.execute('INSERT OR REPLACE INTO confirmed_alarms VALUES(?,?,?)', (str(home), key, opened))
        except Exception:
            result['delivery'] = {'status': 'unavailable'}
    else:
        with state_db(state_path) as db:
            db.execute('DELETE FROM alarms WHERE home=?', (str(home),))
            db.execute('DELETE FROM confirmed_alarms WHERE home=?', (str(home),))
    return result


def post_update_check(homes, gate_path, *, now=None, hooks=None):
    """Read existing history only; idle/unknown coverage cannot certify activation."""
    from hermes_cli.plugins import VALID_HOOKS
    from hermes_constants import profile_name_for_home
    now = time.time() if now is None else now
    hooks = VALID_HOOKS if hooks is None else hooks
    reasons = [] if 'before_turn_end' in hooks else ['core_hook_missing']
    active = []
    missing = []
    for home in homes:
        home = Path(home).resolve()
        profile = profile_name_for_home(home) or 'default'
        try:
            with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as db:
                replied = db.execute("SELECT 1 FROM messages WHERE role='assistant' AND content IS NOT NULL "
                    "AND content != '' AND (tool_calls IS NULL OR tool_calls='[]') "
                    "AND coalesce(display_kind,'') != 'delivery_mirror' "
                    "AND timestamp BETWEEN ? AND ? LIMIT 1", (now - 1800, now)).fetchone()
            if not replied:
                continue
            active.append(profile)
            with closing(sqlite3.connect(Path(gate_path).resolve().as_uri() + '?mode=ro', uri=True)) as gate:
                event = gate.execute('SELECT 1 FROM events WHERE profile=? AND created BETWEEN ? AND ? LIMIT 1',
                                     (profile, now - 1800, now)).fetchone()
            if not event:
                missing.append(profile)
        except sqlite3.Error:
            reasons.append('history_unavailable:' + profile)
    if missing:
        reasons.append('recent_gate_missing:' + ','.join(missing))
    if not active:
        reasons.append('no_recent_reply_evidence')
    return {'ok': not reasons, 'status': 'PASSED' if not reasons else 'FAILED',
            'reasons': reasons, 'active_profiles': active, 'window_seconds': 1800}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--authorize-disable', metavar='APPROVAL_REFERENCE')
    parser.add_argument('--until', type=float, help='Authorization expiry, Unix seconds')
    args = parser.parse_args()
    if args.authorize_disable:
        if args.until is None or not time.time() < args.until <= time.time() + 86400:
            parser.error('--until must be in the next 24 hours')
        authorize_disable(args.home, args.state, reference=args.authorize_disable, expires=args.until)
        print(json.dumps({'authorization_recorded': True}))
        return 0
    try:
        result = check(args.home, args.state)
    except Exception:
        result = {'ok': False, 'reasons': ['health_check_failed']}
        try:
            result['delivery'] = send_alarm({'target': 'gurney', 'reasons': result['reasons'],
                'profile': args.home.name if args.home.parent.name == 'profiles' else 'default',
                'profile_home_sha256': hashlib.sha256(str(args.home.resolve()).encode()).hexdigest()})
        except Exception:
            result['delivery'] = {'status': 'unavailable'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
