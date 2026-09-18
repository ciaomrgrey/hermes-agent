"""Recorded error classes remain failures, never inflated by bookkeeping rows."""
import json
import pytest
from tests.completion_gate_support import load


def test_recorded_error_has_stable_cause_hash_without_private_text(tmp_path):
    plugin = load()
    def failed(_):
        raise TimeoutError('private provider payload')
    gate = plugin.Gate({'enabled': True, 'db_path': str(tmp_path / 'gate.db')}, extract=failed)
    for turn in ('a', 'b'):
        gate.evaluate('private answer', profile='gurney', task_id='task', turn_id=turn)
    rows = gate.events()
    assert len(rows[0]['reason_hashes']) == 1
    assert rows[0]['reason_hashes'] == rows[1]['reason_hashes']
    assert 'private' not in json.dumps(rows)


def test_metrics_window_counts_evaluations_not_escalation_bookkeeping(tmp_path):
    plugin = load()
    gate = plugin.Gate({'enabled': True, 'db_path': str(tmp_path / 'gate.db')}, extract=lambda _: [])
    with gate.connect() as db:
        for created, action, claims in [(99, 'gate_error', []), (100, 'deliver', []),
                (101, 'gate_error', [{'verdict': 'unverified'}]), (102, 'escalation_queued', []),
                (103, 'advice_advised', []), (104, 'reentrant_pass', []), (105, 'block', [])]:
            row = gate._append(db, 'p', 'task', str(created), action, claims, 0)
            db.execute('UPDATE events SET created=? WHERE id=?', (created, row))
    metrics = gate.metrics(since=100, until=106)
    assert metrics['evaluations'] == 3
    assert metrics['gate_errors'] == 1
    assert metrics['gate_error_rate'] == pytest.approx(1 / 3)
    assert metrics['unverified_evaluation_rate'] == pytest.approx(1 / 3)
    assert metrics['per_profile']['p']['gate_errors'] == 1
    assert metrics['reentrant_passes'] == 1
    assert gate.metrics(since=200, until=300)['gate_error_rate'] is None
