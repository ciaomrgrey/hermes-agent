"""Dispatch-attempt budgets are durable across every requeue path."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_transfer as kt
from tools import kanban_tools


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


def test_attempt_cap_can_be_set_and_cleared_through_cli(conn, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    task_id = kb.create_task(conn, title="bounded later", assignee="builder")

    created = kc.run_slash("create 'bounded from cli' --assignee builder --max-attempts 2 --json")
    created_id = __import__("json").loads(created)["id"]
    assert kb.get_task(conn, created_id).max_attempts == 2
    assert "Set attempt cap" in kc.run_slash(f"set-attempts {task_id} 1")
    assert kb.get_task(conn, task_id).max_attempts == 1
    assert "Cleared attempt cap" in kc.run_slash(f"set-attempts {task_id} none")
    assert kb.get_task(conn, task_id).max_attempts is None


def test_tool_show_reads_back_attempt_cap(conn, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    task_id = kb.create_task(conn, title="bounded", assignee="builder", max_attempts=2)

    payload = __import__("json").loads(kanban_tools._handle_show({"task_id": task_id}))

    assert payload["task"]["max_attempts"] == 2


@pytest.mark.parametrize("outcome", ["timed_out", "crashed", "protocol_violation"])
def test_failed_worker_runs_charge_attempt_cap(conn, outcome: str) -> None:
    task_id = kb.create_task(conn, title=outcome, assignee="builder", max_attempts=1)
    assert kb.claim_task(conn, task_id, claimer="builder:first") is not None
    assert not kbd._record_task_failure(
        conn,
        task_id,
        outcome,
        outcome=outcome,
        failure_limit=3,
        release_claim=True,
        end_run=True,
    )

    assert kb.claim_task(conn, task_id, claimer="builder:second") is None
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"


def test_infrastructure_spawn_failure_does_not_charge_attempt_cap(conn) -> None:
    task_id = kb.create_task(conn, title="spawn refusal", assignee="builder", max_attempts=1)
    assert kb.claim_task(conn, task_id, claimer="dispatcher:first") is not None
    assert not kbd._record_task_failure(
        conn,
        task_id,
        "profile cannot be spawned",
        outcome="spawn_failed",
        failure_limit=3,
        release_claim=True,
        end_run=True,
    )

    assert kb.claim_task(conn, task_id, claimer="dispatcher:second") is not None
    assert len(kb.list_runs(conn, task_id)) == 2


def test_manual_unblock_cannot_renew_an_exhausted_attempt(conn) -> None:
    task_id = kb.create_task(conn, title="no renewal", assignee="builder", max_attempts=1)
    assert kb.claim_task(conn, task_id) is not None
    assert kb.reclaim_task(conn, task_id, reason="worker crashed", signal_fn=lambda *_: None)
    assert kb.claim_task(conn, task_id) is None
    assert kb.unblock_task(conn, task_id)

    assert kb.claim_task(conn, task_id) is None
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert len(kb.list_runs(conn, task_id)) == 1


def test_reassigning_a_to_b_to_a_does_not_reset_attempt_history(conn) -> None:
    task_id = kb.create_task(conn, title="profile round trip", assignee="a", max_attempts=1)
    assert kb.claim_task(conn, task_id) is not None
    assert kb.reclaim_task(conn, task_id, reason="handoff", signal_fn=lambda *_: None)
    assert kb.assign_task(conn, task_id, "b")
    assert kb.claim_task(conn, task_id) is not None
    assert kb.reclaim_task(conn, task_id, reason="handoff back", signal_fn=lambda *_: None)
    assert kb.assign_task(conn, task_id, "a")

    assert kb.claim_task(conn, task_id) is None
    assert [(run.profile, run.outcome) for run in kb.list_runs(conn, task_id)] == [
        ("a", "reclaimed"),
        ("b", "reclaimed"),
    ]


def test_administrative_hold_does_not_spend_first_dispatch(conn) -> None:
    task_id = kb.create_task(conn, title="operator hold", assignee="builder", max_attempts=1)
    assert kb.block_task(conn, task_id, reason="operator hold", kind="needs_input")
    assert kb.unblock_task(conn, task_id)
    assert not [event for event in kb.list_events(conn, task_id) if event.kind == "claimed"]
    assert len(kb.list_runs(conn, task_id)) == 1

    assert kb.claim_task(conn, task_id) is not None


def test_claimed_worker_block_spends_attempt_after_machine_state_scrub(conn) -> None:
    task_id = kb.create_task(conn, title="worker block", assignee="builder", max_attempts=1)
    assert kb.claim_task(conn, task_id) is not None
    assert kb.block_task(conn, task_id, reason="worker needs input", kind="needs_input")
    with kb.write_txn(conn):
        kt._scrub_local_state(conn)
    assert kb.list_runs(conn, task_id)[0].claim_lock is None
    assert kb.unblock_task(conn, task_id)

    assert kb.claim_task(conn, task_id) is None
    assert len(kb.list_runs(conn, task_id)) == 1


def test_legacy_schema_migrates_attempt_cap_as_unlimited(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    initialized = kbc.connect(db_path)
    task_id = kb.create_task(initialized, title="old task", assignee="builder")
    initialized.close()
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    legacy.execute("ALTER TABLE tasks DROP COLUMN max_attempts")
    legacy.commit()
    kbc._migrate_add_optional_columns(legacy)
    legacy.commit()
    kbc._migrate_add_optional_columns(legacy)
    legacy.commit()
    legacy.close()

    migrated = sqlite3.connect(db_path)
    migrated.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(tasks)")}
        assert "max_attempts" in columns
        task = kb.get_task(migrated, task_id)
        assert task is not None
        assert task.max_attempts is None
    finally:
        migrated.close()
