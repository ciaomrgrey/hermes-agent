"""Recorded review_requested/run410 and changes_requested/run416 regressions."""
import sqlite3
import pytest
from agent.kanban_stop import build_kanban_stop_nudge
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def worker(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kb.init_db(path)
    with kbc.connect(path) as db:
        tid = kb.create_task(db, title='test', assignee='cody')
        task = kb.claim_task(db, tid, claimer='cody:test')
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    return path, tid, task.current_run_id


def test_native_review_handoff_stops_old_run_even_after_reclaim(worker, monkeypatch):
    path, tid, run = worker
    assert build_kanban_stop_nudge(messages=[]) is not None
    with kbc.connect(path) as db:
        assert kb.request_review(db, tid, summary='test', reviewer='gurney', expected_run_id=run)
    assert build_kanban_stop_nudge(messages=[]) is None
    with kbc.connect(path) as db:
        review = kb.claim_review_task(db, tid, claimer='gurney:test')
    assert build_kanban_stop_nudge(messages=[]) is None
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(review.current_run_id))
    # A failed/attempted tool call is not a terminal transition.
    attempted = [{'role': 'assistant', 'tool_calls': [{'function': {'name': 'kanban_complete'}}]}]
    assert build_kanban_stop_nudge(messages=attempted) is not None
    with kbc.connect(path) as db:
        assert kb.request_changes(db, tid, reason='test', expected_run_id=review.current_run_id)
        implementation = kb.claim_task(db, tid, claimer='cody:next')
        assert implementation.current_run_id != review.current_run_id
    assert build_kanban_stop_nudge(messages=[]) is None
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(implementation.current_run_id))
    nudge = build_kanban_stop_nudge(messages=[])
    assert nudge is not None
    assert 'kanban_request_review' in nudge
    assert 'kanban_request_changes' in nudge


def test_unavailable_board_does_not_claim_running_or_demand_mutation(worker, monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'missing.db'))
    nudge = build_kanban_stop_nudge(messages=[])
    assert 'kanban_show' in nudge
    assert 'is still `running`' not in nudge
    assert not (tmp_path / 'missing.db').exists()
