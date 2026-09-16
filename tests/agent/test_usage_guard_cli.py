"""Exercise the real CLI in a subprocess without provider or Telegram access."""
import json
import subprocess
import sys
from pathlib import Path


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
