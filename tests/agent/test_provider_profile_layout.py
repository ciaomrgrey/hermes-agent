"""Native profile config writer and fresh policy reader; no live providers."""
import json
import os
from pathlib import Path
import subprocess
import sys

from agent.usage_guard import Guard


def test_profile_cli_config_and_runtime_share_canonical_hold(tmp_path):
    root = tmp_path / 'estate'
    database = root / 'usage-guard.sqlite3'
    root.mkdir()
    with Guard(database, {}).db as conn:
        conn.execute('INSERT INTO holds VALUES(?,?,?,?)',
                     ('openai-codex', 1, 'synthetic-hold', '*'))
    source = Path(__file__).resolve().parents[2]
    entry = [sys.executable, '-c', 'from hermes_cli.main import main; main()']
    for profile in ('generalist', 'cody'):
        home = root / 'profiles' / profile
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('{}\n')
        env = dict(os.environ, HERMES_HOME=str(home))
        for key, value in [('provider_control.database', str(database)),
                           ('provider_control.providers', '["anthropic", "openai-codex"]')]:
            proc = subprocess.run(entry + ['-p', profile, 'config', 'set', key, value],
                                  cwd=source, env=env, text=True, capture_output=True, timeout=30)
            assert proc.returncode == 0, proc.stdout + proc.stderr
        # New process, like a gateway/cron/delegate child: no parent loader cache.
        proc = subprocess.run([sys.executable, '-c', '''
import json
from agent.provider_control import current_policy, HeldProvider
from hermes_constants import get_hermes_home
policy = current_policy()
try:
    policy.check('openai-codex')
except HeldProvider:
    held = True
else:
    held = False
policy.check('anthropic')
print(json.dumps(dict(home=str(get_hermes_home()), database=str(policy.database), held=held)))
'''], cwd=source, env=env, text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        receipt = json.loads(proc.stdout)
        assert receipt == dict(home=str(home), database=str(database), held=True)
    assert not (root / 'config.yaml').exists(), 'profile writes must not leak into root'
