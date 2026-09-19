"""Workerless administrative blocks are not worker retry attempts."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.mark.parametrize("kind", [None, "needs_input", "capability", "transient", "dependency"])
@pytest.mark.parametrize("preblock", [False, True])
@pytest.mark.parametrize("reason", [None, "Administrative hold"])
def test_only_worker_attempts_trip_loop_guard(tmp_path, monkeypatch, kind, preblock, reason):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Human decision", assignee="worker")
        kb.recompute_ready(conn)
        if preblock:
            assert kb.block_task(conn, tid, reason=reason, kind=kind)
            task = kb.get_task(conn, tid)
            assert task and task.status == ("todo" if kind == "dependency" else "blocked")
            if kind != "dependency":
                assert kb.unblock_task(conn, tid)
            kb.recompute_ready(conn)
        for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            assert kb.block_task(conn, tid, reason="Asked; waiting", kind=kind)
            expected = "todo" if kind == "dependency" else (
                "triage" if attempt + 1 == kb.BLOCK_RECURRENCE_LIMIT else "blocked")
            task = kb.get_task(conn, tid)
            assert task and task.status == expected
            if expected == "blocked":
                assert kb.unblock_task(conn, tid)
                kb.recompute_ready(conn)
                # An interposed administrative block must not erase genuine attempts.
                assert kb.block_task(conn, tid, reason=reason, kind=kind)
                task = kb.get_task(conn, tid)
                assert task and task.status == "blocked"
                assert kb.unblock_task(conn, tid)
            kb.recompute_ready(conn)


@pytest.mark.parametrize("claimed", [False, True])
def test_legacy_block_provenance_preserves_worker_guard(tmp_path, monkeypatch, claimed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy block", assignee="worker")
        kb.recompute_ready(conn)
        if claimed:
            assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="Pre-block", kind="needs_input")
        # Persist the pre-fix shape. A same-second claim still counts without a PID.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_recurrences=1 WHERE id=?", (tid,))
            conn.execute("UPDATE task_runs SET ended_at=started_at WHERE task_id=?", (tid,))
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='blocked'",
                         ('{"kind":"needs_input","recurrences":1}', tid))
        assert kb.unblock_task(conn, tid)
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="Owner asked", kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task and task.status == ("triage" if claimed else "blocked")
