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
