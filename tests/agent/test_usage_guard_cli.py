"""Exercise the real CLI in a subprocess without provider or Telegram access."""
import json
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_report_drains_retries_with_native_receipts(tmp_path, monkeypatch, capsys):
    """Real CLI dispatch and SQLite; only external Telegram I/O is replaced."""
    import tools.send_message_tool as transport
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root/'config.json').read_text())
    config['delivery_enabled'] = True
    path = tmp_path/'config.json'
    path.write_text(json.dumps(config))
    messages = []
    def send(args):
        messages.append(args)
        return json.dumps({'success': len(messages) > 1, 'message_id': 'local-fixture-1' if len(messages) > 1 else None})
    monkeypatch.setattr(transport, 'send_message_tool', send)
    monkeypatch.setattr(sys, 'argv', ['usage_guard.py', 'report', '--config', str(path)])
    for _ in range(3):
        runpy.run_path(str(root/'usage_guard.py'), run_name='__main__')
        json.loads(capsys.readouterr().out)
    assert len(messages) == 2, 'CLI must retry failed notice, then stop after receipt'
    assert messages[0] == messages[1]
    assert messages[0]['target'] == 'telegram:471605389'
    assert 'unavailable' in messages[0]['message']
    assert 'unsupported' in messages[0]['message']
    with sqlite3.connect(tmp_path/'state.sqlite3') as db:
        attempts, receipt = db.execute('SELECT attempts,receipt FROM outbox').fetchone()
    assert attempts == 2
    assert json.loads(receipt)['message_id'] == 'local-fixture-1'


def test_cli_breach_persists_hold_before_failed_delivery(tmp_path, monkeypatch, capsys):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agent.usage_guard import Guard
    from agent import usage_guard_sources as sources
    import tools.send_message_tool as transport
    import time
    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root/'config.json').read_text())
    cfg['delivery_enabled'] = True
    path = tmp_path/'config.json'
    path.write_text(json.dumps(cfg))
    now = datetime(2026, 9, 16, 23, 0, tzinfo=ZoneInfo('Europe/Zurich')).timestamp()
    value = dict(status='ok', grant='synthetic', reset=now+86400, pp=0)
    guard = Guard(tmp_path/'state.sqlite3', cfg)
    guard.sample('anthropic', now-60, value)
    guard.close()
    monkeypatch.setattr(time, 'time', lambda: now)
    monkeypatch.setattr(sources, 'fetch', lambda p: dict(value, pp=11, fetched_at=now) if p == 'anthropic' else {'status':'unavailable'})
    sent = []
    def send(args):
        with sqlite3.connect(tmp_path/'state.sqlite3') as db:
            assert db.execute('SELECT provider FROM holds').fetchall() == [('anthropic',)]
        sent.append(args['message'])
        return json.dumps({'success':False})
    monkeypatch.setattr(transport, 'send_message_tool', send)
    monkeypatch.setattr(sys, 'argv', ['usage_guard.py', 'sample', '--config', str(path)])
    runpy.run_path(str(root/'usage_guard.py'), run_name='__main__')
    result = json.loads(capsys.readouterr().out)
    assert result['anthropic']['rolling']['pp'] == 11
    assert any('+11.00 pp' in message for message in sent)
    guard = Guard(tmp_path/'state.sqlite3', cfg)
    assert guard.holds() == ['anthropic']
    assert all(row['attempts'] == 1 and row['receipt'] is None for row in guard.outbox())
    guard.close()


def test_delivery_flag_rejects_truthy_non_boolean(tmp_path):
    from usage_guard import load_config
    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root/'config.json').read_text())
    cfg['delivery_enabled'] = 'false'
    path = tmp_path/'config.json'
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match='delivery_enabled'):
        load_config(path)


def test_status_and_report_cli(tmp_path):
    root = Path(__file__).resolve().parents[2]
    assert (root/'usage_guard.py').exists(), 'CLI missing'
    cfg = dict(threshold_pp=10, window_seconds=21600, sample_seconds=60,
               timezone='Europe/Zurich', night_start='22:00', night_end='06:00',
               report_time='06:05', telegram_target='telegram:471605389', armed=False,
               retention_days=7)
    config = tmp_path/'config.json'
    config.write_text(json.dumps(cfg))
    def run(command):
        result = subprocess.run([sys.executable, str(root/'usage_guard.py'), command,
                                 '--config', str(config)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    status = run('status')
    assert status['armed'] is False
    assert status['release_ready'] is False
    assert status['held'] == []
    assert run('report')['providers']['anthropic']['peak_pp'] is None
    assert run('status')['pending_alerts'] == 1
    cfg['armed'] = True
    config.write_text(json.dumps(cfg))
    result = subprocess.run([sys.executable, str(root/'usage_guard.py'), 'sample', '--config', str(config)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert 'request-owned auxiliary cancellation' in result.stderr
