"""Finite delivered history, lossless pending incidents, honest saturation."""
import json
from agent.usage_guard import Guard


def test_retention_preserves_original_pending_and_current_dedupe(tmp_path):
    cfg = dict(threshold_pp=10,window_seconds=21600,sample_seconds=60,timezone='Europe/Zurich',
               night_start='22:00',night_end='06:00',retention_days=7,pending_warning_count=2)
    path=tmp_path/'state.sqlite3'
    guard=Guard(path,cfg)
    row=dict(status='ok',pp=0,grant='synthetic',reset=604800)
    guard.sample('anthropic',0,row)
    guard.sample('anthropic',60,dict(row,pp=11))
    original=guard.outbox()[0]
    guard.report(3600)
    guard.deliver(lambda body: 'fixture-receipt' if json.loads(body)['id'].startswith('report:') else None)
    guard.report(8*86400)
    guard.maintain(8*86400)
    assert guard.db.execute('SELECT COUNT(*) FROM outbox WHERE receipt IS NOT NULL').fetchone()[0]==0
    assert guard.outbox()[0]['body']==original['body']
    status=guard.storage_status()
    assert status['pending']==2 and status['saturated'] is True
    assert status['pending_policy']=='retain-originals; total-storage-not-bounded-during-indefinite-outage'
    guard.close()
    guard=Guard(path,cfg)
    assert guard.outbox()[0]['body']==original['body']
    guard.deliver(lambda body:'fixture-recovered')
    assert guard.storage_status()['pending']==0
    guard.maintain(16*86400)
    assert guard.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    # Existing active incident ID remains until a sampled nonbreach or epoch change.
    assert guard.db.execute('SELECT id FROM incidents').fetchone()[0]==original['id']
    guard.close()


def test_cli_runs_retention_and_exposes_pending_saturation(tmp_path,monkeypatch,capsys):
    import sys
    import usage_guard as cli
    cfg = dict(threshold_pp=10,window_seconds=21600,sample_seconds=60,timezone='Europe/Zurich',
               night_start='22:00',night_end='06:00',retention_days=7,pending_warning_count=1,
               report_time='06:05',telegram_target='telegram:471605389',armed=False,delivery_enabled=False)
    path=tmp_path/'config.json'
    path.write_text(json.dumps(cfg))
    guard=Guard(tmp_path/'state.sqlite3',cfg)
    guard.report(0)
    guard.deliver(lambda body:'fixture-receipt')
    guard.close()
    monkeypatch.setattr(cli.time,'time',lambda:8*86400)
    monkeypatch.setattr(sys,'argv',['usage_guard.py','report','--config',str(path)])
    cli.main()
    capsys.readouterr()
    guard=Guard(tmp_path/'state.sqlite3',cfg)
    assert guard.db.execute('SELECT COUNT(*) FROM outbox WHERE receipt IS NOT NULL').fetchone()[0]==0
    guard.close()
    monkeypatch.setattr(sys,'argv',['usage_guard.py','status','--config',str(path)])
    cli.main()
    status=json.loads(capsys.readouterr().out)
    assert status['storage']['saturated'] is True

