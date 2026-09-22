"""Dispatch-attempt budgets are durable across every requeue path."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def test_one_attempt_cap_blocks_crash_reclaims_before_another_claim(conn) -> None:
    task_id = kb.create_task(conn, title="one shot", assignee="builder", max_attempts=1)
    first = kb.claim_task(conn, task_id, claimer="builder:first")
    assert first is not None

    assert kb.reclaim_task(conn, task_id, reason="worker crashed", signal_fn=lambda *_: None)
    assert kb.get_task(conn, task_id).status == "ready"

    assert kb.claim_task(conn, task_id, claimer="builder:second") is None
    assert kb.claim_task(conn, task_id, claimer="builder:third") is None

    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    assert task.worker_pid is None
    assert task.current_run_id is None
    runs = kb.list_runs(conn, task_id)
    assert [(run.profile, run.outcome) for run in runs] == [("builder", "reclaimed")]
    exhausted = [event for event in kb.list_events(conn, task_id) if event.kind == "attempt_limit_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0].payload == {
        "profile": "builder",
        "attempts": 1,
        "max_attempts": 1,
        "reason": "dispatch attempt limit exhausted for profile builder: 1/1 claimed runs",
    }


def test_attempt_cap_is_profile_scoped_across_review_a_b_a(conn) -> None:
    task_id = kb.create_task(conn, title="reviewed one shot", assignee="builder", max_attempts=1)
    implementation = kb.claim_task(conn, task_id, claimer="builder:first")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )

    review = kb.claim_review_task(conn, task_id, claimer="reviewer:first")
    assert review is not None
    ok, owner = kb.request_changes(
        conn,
        task_id,
        reason="fix this",
        expected_run_id=review.current_run_id,
    )
    assert (ok, owner) == (True, "builder")

    assert kb.claim_task(conn, task_id, claimer="builder:second") is None
    assert kb.get_task(conn, task_id).status == "blocked"
    assert [(run.profile, run.outcome) for run in kb.list_runs(conn, task_id)] == [
        ("builder", "review_requested"),
        ("reviewer", "changes_requested"),
    ]


def test_unlimited_review_fix_flow_and_parent_gate_are_unchanged(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="lead")
    task_id = kb.create_task(conn, title="normal", assignee="builder", parents=[parent])
    assert kb.claim_task(conn, task_id) is None
    assert kb.get_task(conn, task_id).status == "todo"

    assert kb.complete_task(conn, parent, result="done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task_id).status == "ready"
    implementation = kb.claim_task(conn, task_id, claimer="builder:first")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:first")
    assert review is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason="fix this",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    assert kb.claim_task(conn, task_id, claimer="builder:second") is not None


def test_concurrent_dispatchers_cannot_exceed_attempt_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    setup = kbc.connect(db_path)
    task_id = kb.create_task(setup, title="raced", assignee="builder", max_attempts=1)
    setup.close()
    barrier = threading.Barrier(2)
    results: list[object] = []

    def claim(name: str) -> None:
        conn = kbc.connect(db_path)
        try:
            barrier.wait()
            results.append(kb.claim_task(conn, task_id, claimer=name))
        finally:
            conn.close()

    threads = [threading.Thread(target=claim, args=(f"dispatcher:{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    verify = kbc.connect(db_path)
    try:
        assert sum(result is not None for result in results) == 1
        assert len(kb.list_runs(verify, task_id)) == 1
        task = kb.get_task(verify, task_id)
        assert task.status == "running"
        assert task.current_run_id is not None
    finally:
        verify.close()
