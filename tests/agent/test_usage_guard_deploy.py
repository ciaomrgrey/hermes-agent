"""Inactive native scheduling template, exercised only in a temporary cron store."""
import json
from pathlib import Path


def test_native_template_paused_creation_resume_and_rollback(tmp_path):
    from cron.jobs import create_job, get_job, resume_job, remove_job, use_cron_store
    spec = json.loads((Path(__file__).resolve().parents[2]/'deploy/cron-create.json').read_text())
    with use_cron_store(tmp_path/'cron'):
        job = create_job(**spec)
        saved = get_job(job['id'])
        assert saved['no_agent'] is True
        assert saved['enabled'] is False and saved['next_run_at'] is None
        assert saved['provider_snapshot'] is None and saved['model_snapshot'] is None
        assert saved['deliver'] == 'local'
        assert saved['failure_deliver'] == 'telegram:471605389'
        resumed = resume_job(job['id'])
        assert resumed['enabled'] and resumed['next_run_at']
        assert remove_job(job['id'])
        assert get_job(job['id']) is None


def test_tick_wrapper_runs_real_cli_under_profile_home(tmp_path, monkeypatch, capsys):
    import runpy
    import sqlite3
    import shutil
    import usage_guard as cli
    from hermes_constants import get_hermes_home
    root = Path(__file__).resolve().parents[2]
    home = get_hermes_home()
    data = home/'usage-guard'
    data.mkdir(parents=True)
    shutil.copyfile(root/'config.json', data/'config.json')
    monkeypatch.setattr(cli, 'fetch', lambda provider: dict(status='unsupported', reason='synthetic offline fixture'))
    runpy.run_path(str(root/'deploy/usage-guard-tick.py'), run_name='__main__')
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {'anthropic','openai-codex','xai-oauth'}
    with sqlite3.connect(data/'state.sqlite3') as conn:
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM holds').fetchone()[0] == 0
