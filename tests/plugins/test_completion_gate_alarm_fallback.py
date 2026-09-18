"""Exercise alarm fallback without sending estate test messages."""
import importlib.util
import json
from pathlib import Path
import subprocess
import pytest


def health():
    spec = importlib.util.spec_from_file_location('health_fallback', Path(__file__).resolve().parents[2] / 'external/completion-gate-health.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('status', ['unavailable', 'unverified', 'exception'])
def test_unreachable_owner_falls_back_to_explicit_telegram(tmp_path, monkeypatch, status):
    from hermes_cli import profiles
    from tools import bot_live_delivery as live
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda _: tmp_path)
    monkeypatch.setattr(live, 'find_canonical_live_owner', lambda _: None)
    def receipt(*args):
        if status == 'exception':
            raise OSError('unreachable')
        if status == 'unavailable':
            return None

        return {'status': 'queued', 'message': 'wrong message'}
    monkeypatch.setattr(live, 'read_delivery_result', receipt)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps({'success': True, 'message_id': 'test-only'}), '')
    monkeypatch.setattr(subprocess, 'run', run)
    result = health().send_alarm({'profile': 'cody', 'reasons': ['core_hook_missing']})
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert 'telegram:471605389' in argv
    assert '-p' in argv and 'generalist' in argv
    assert 'cody' in kwargs['input'] and 'core_hook_missing' in kwargs['input']
    assert '\n' not in kwargs['input']
    assert result['status'] == 'sent'
    assert result['transport'] == 'telegram'
