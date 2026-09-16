"""Disposable native config/cron installation and no_agent runtime.

Provider values are explicitly synthetic via child startup fixture. No delivery
is enabled and the actual native script runner/CLI/SQLite are exercised.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from agent.usage_guard import Guard


def test_disposable_native_install_run_held_restart_and_rollback(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from cron.jobs import create_job, get_job, resume_job, remove_job, use_cron_store
    from cron.scheduler import run_job
    from agent import provider_control as pc
    home = get_hermes_home()
    root = Path(__file__).resolve().parents[2]
    data, scripts, offline = home/'usage-guard', home/'scripts', tmp_path/'offline'
    for path in (data, scripts, offline):
        path.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((root/'config.json').read_text())
    assert cfg['armed'] is False and cfg['delivery_enabled'] is False
    (data/'config.json').write_text(json.dumps(cfg))
    shutil.copyfile(root/'deploy/usage-guard-tick.py', scripts/'usage-guard-tick.py')
    with Guard(data/'state.sqlite3', cfg).db as conn:
        conn.execute('INSERT INTO holds VALUES(?,?,?,?)', ('openai-codex', 1, 'synthetic-hold', '*'))
    # Source substitution is mandatory and recorded; never enable real delivery.
    (offline/'sitecustomize.py').write_text('''
import json
from pathlib import Path
import usage_guard
from hermes_constants import get_hermes_home
from agent.provider_control import current_policy, HeldProvider
try:
    current_policy().check('openai-codex')
except HeldProvider:
    held = True
else:
    held = False
usage_guard.fetch = lambda provider: dict(status='unsupported', reason='synthetic install fixture')
(get_hermes_home()/'child-install-receipt.json').write_text(json.dumps(dict(
    held=held, source=usage_guard.__file__, home=str(get_hermes_home()))))
''')
    # Install into an isolated interpreter: cron does not promise checkout PYTHONPATH.
    import venv
    installed = tmp_path/'installed'
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(installed)
    python = installed/'bin/python'
    # Reuse this test interpreter's installed dependencies without altering them.
    import sysconfig
    purelib = Path(sysconfig.get_path('purelib'))
    installed_site = installed/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    (installed_site/'fixture-dependencies.pth').write_text(str(purelib)+'\n')
    install = subprocess.run([str(python), '-m', 'pip', 'install', '--no-deps',
                              '--no-build-isolation', '-e', str(root)],
                             text=True, capture_output=True, timeout=90)
    assert install.returncode == 0, install.stdout+install.stderr
    monkeypatch.setattr(sys, 'executable', str(python))
    env = dict(os.environ, PYTHONPATH=str(offline))
    # Actual native setting command in the disposable home, not a fake config loader.
    entry = [sys.executable, '-c', 'from hermes_cli.main import main; main()']
    for key, value in [('provider_control.database', str(data/'state.sqlite3')),
                       ('provider_control.providers', '["anthropic", "openai-codex"]')]:
        proc = subprocess.run(entry+['config', 'set', key, value], env=env, cwd=root,
                              text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, proc.stdout+proc.stderr
    monkeypatch.setenv('PYTHONPATH', env['PYTHONPATH'])
    import pytest
    with pytest.raises(pc.HeldProvider):
        pc.current_policy().check('openai-codex')
    pc.current_policy().check('anthropic')
    spec = json.loads((root/'deploy/cron-create.json').read_text())
    with use_cron_store(home/'cron'):
        job = create_job(**spec)
        saved = get_job(job['id'])
        assert saved is not None and not saved['enabled']
        job = resume_job(job['id'])  # disposable store only
        assert job is not None
        for _ in range(2):  # real fresh child process on each native run
            ok, document, response, error = run_job(job)
            assert ok and error is None, document
            result = json.loads(response)
            assert all(v['source']['reason']=='synthetic install fixture' for v in result.values())
            receipt = json.loads((home/'child-install-receipt.json').read_text())
            assert receipt['held'] is True
            assert Path(receipt['source']).resolve() == root/'usage_guard.py'
            assert Path(receipt['home']).resolve() == home.resolve()
        with sqlite3.connect(data/'state.sqlite3') as conn:
            assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 6
            assert conn.execute('SELECT COUNT(*) FROM holds').fetchone()[0] == 1
        assert remove_job(job['id']) and get_job(job['id']) is None
    # Rollback de-wires future admissions without deleting resumable control/history.
    proc = subprocess.run(entry+['config','set','provider_control.providers','[]'],
                          env=env, cwd=root, text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout+proc.stderr
    pc.current_policy().check('openai-codex')
    assert (data/'state.sqlite3').exists()
