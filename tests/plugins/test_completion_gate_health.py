"""Independent health check survives absent/disabled gate; no estate sends."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def checker():
    path = ROOT / 'external/completion-gate-health.py'
    assert path.exists(), 'independent post-update checker missing'
    spec = importlib.util.spec_from_file_location('cg_health', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def config(home, enabled=True):
    home.mkdir(exist_ok=True)
    (home / 'config.yaml').write_text(json.dumps({'plugins': {
        'enabled': ['completion-gate'], 'entries': {'completion-gate': {
            'settings': {'enabled': enabled}}}}}))


def test_missing_package_alert_is_loud_and_retries_unavailable(tmp_path, monkeypatch):
    health = checker()
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, 'get_bundled_plugins_dir', lambda: tmp_path / 'wiped')
    home = tmp_path / 'profile'
    config(home)
    receipts = []
    def send(event):
        receipts.append(event)
        return {'status': 'unavailable'}
    for _ in range(2):
        result = health.check(home, tmp_path / 'health.db', now=1000, send=send)
        assert result['ok'] is False
        assert 'package_unavailable' in result['reasons']
    assert len(receipts) == 2
    assert all(e['target'] == 'gurney' for e in receipts)
    assert result['delivery']['status'] == 'unavailable'


def test_disabled_without_exact_authorization_alerts_even_first_check(tmp_path, monkeypatch):
    health = checker()
    home = tmp_path / 'profile'
    config(home, enabled=False)
    sent = []
    state = tmp_path / 'health.db'
    notify = lambda e: sent.append(e) or {'status': 'queued'}
    result = health.check(home, state, now=1000, send=notify)
    assert 'unauthorized_disable' in result['reasons']
    # An explicit bounded authorization records the exact disabled config, not prose inference.
    health.authorize_disable(home, state, reference='owner-approved-test', expires=1200)
    assert health.check(home, state, now=1100, send=notify)['ok'] is True
    assert 'unauthorized_disable' in health.check(home, state, now=1300, send=notify)['reasons']
    # Removing allow-list also alerts; previous authorized bytes cannot bless another change.
    (home / 'config.yaml').write_text('{"plugins":{"enabled":[]}}')
    assert 'unauthorized_disable' in health.check(home, state, now=1100, send=notify)['reasons']


def test_liveness_requires_native_final_activity_and_same_profile_gate_events(tmp_path, monkeypatch):
    import sqlite3
    from hermes_state import SessionDB
    from hermes_constants import profile_name_for_home
    from tests.completion_gate_support import install
    health = checker()
    home = tmp_path / 'profiles' / 'cody'
    home.mkdir(parents=True)
    config(home)
    install(home)
    gate_path = tmp_path / 'gate.db'
    cfg = json.loads((home / 'config.yaml').read_text())
    cfg['plugins']['entries']['completion-gate']['settings']['db_path'] = str(gate_path)
    (home / 'config.yaml').write_text(json.dumps(cfg))
    state = tmp_path / 'health.db'
    send = lambda e: {'status': 'queued'}
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s', source='cli', model='test')
    try:
        # Idle time alone is never a dead-gate alarm.
        assert health.check(home, state, now=1000, send=send)['ok']
        db.append_message('s', 'assistant', content='final delivered', finish_reason='stop')
        with sqlite3.connect(home / 'state.db') as conn:
            conn.execute('UPDATE messages SET timestamp=1010')
        result = health.check(home, state, now=1200, send=send)
        assert 'liveness_gap' in result['reasons']
        # A second profile writing the shared database cannot mask this profile's silence.
        with sqlite3.connect(gate_path) as conn:
            conn.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, created REAL, profile TEXT)')
            conn.execute("INSERT INTO events VALUES(1,1005,'other')")
        assert 'liveness_gap' in health.check(home, state, now=1201, send=send)['reasons']
        with sqlite3.connect(gate_path) as conn:
            conn.execute('INSERT INTO events VALUES(2,1005,?)', (profile_name_for_home(home) or 'default',))
        assert health.check(home, state, now=1202, send=send)['ok']
        # Unchanged healthy output is not evidence for the next delivered turn.
        db.append_message('s', 'assistant', content='new final', finish_reason='stop')
        with sqlite3.connect(home / 'state.db') as conn:
            conn.execute('UPDATE messages SET timestamp=1300 WHERE id=(SELECT max(id) FROM messages)')
        assert 'liveness_gap' in health.check(home, state, now=1500, send=send)['reasons']
    finally:
        db.close()


def test_native_alarm_receipt_rearms_after_recovery_and_cli_fails_loudly(tmp_path, monkeypatch, capsys):
    import sys
    from hermes_cli import profiles, plugins
    from tools import bot_live_delivery as live
    from tests.completion_gate_support import install
    health = checker()
    home = tmp_path / 'profile'
    config(home)
    target = tmp_path / 'gurney'
    target.mkdir()
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: target if name == 'gurney' else None)
    owner = dict(profile_home=str(target.resolve()), session_id='s', lease_id='lease', live_session_id='live')
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda _: owner)
    monkeypatch.setattr(plugins, 'get_bundled_plugins_dir', lambda: tmp_path / 'wiped')
    state = tmp_path / 'health.db'
    first = health.check(home, state, now=1000.0)
    receipt = live.read_delivery_result(target, first['delivery']['delivery_id'])
    assert receipt['status'] == 'queued'
    assert 'package_unavailable' in receipt['message']
    package = install(home)
    assert health.check(home, state, now=1100)['ok']
    # A real import failure after recovery is a NEW alarm, not deduped forever.
    (package / '__init__.py').write_text('raise ImportError("synthetic missing dependency")\n')
    second = health.check(home, state, now=1200)
    assert first['reasons'] == second['reasons'] == ['package_unavailable']
    assert second['delivery']['delivery_id'] != first['delivery']['delivery_id']
    monkeypatch.setattr(sys, 'argv', ['health', '--home', str(home), '--state', str(state)])
    assert health.main() == 1
    assert json.loads(capsys.readouterr().out)['ok'] is False


@pytest.mark.macos_only
def test_shared_external_link_is_profile_local_and_missing_core_is_loud(tmp_path, monkeypatch):
    import shutil
    from hermes_cli import plugins
    from tests.completion_gate_support import SOURCE
    health = checker()
    shared = tmp_path / 'shared/completion-gate'
    shutil.copytree(SOURCE, shared)
    a, b = tmp_path / 'a', tmp_path / 'b'
    for home, enabled in ((a, True), (b, False)):
        config(home, enabled=enabled)
        (home / 'plugins').mkdir()
        (home / 'plugins/completion-gate').symlink_to(shared, target_is_directory=True)
    send = lambda e: {'status': 'queued'}
    for home, expected in ((a, True), (b, False), (a, True)):
        assert health.check(home, tmp_path / 'health.db', send=send)['ok'] is expected
    monkeypatch.setattr(plugins, 'VALID_HOOKS', plugins.VALID_HOOKS - {'before_turn_end'})
    assert 'core_hook_missing' in health.check(a, tmp_path / 'health.db', send=send)['reasons']
