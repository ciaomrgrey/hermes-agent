"""``archive_task(refuse_live_claim=True)``: the live-claim refusal and the reason are atomic with the archive.

A caller that reads the row, sees no live claim and then calls ``archive_task`` races the dispatcher:
a claim landing in between was archived and its worker terminated. The refusal therefore lives inside
the archive transaction. Estate consumer: the ``kanban_archive`` orchestrator tool (plugin).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    yield kb, conn
    conn.close()


def _events(kb, conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _live_claim(kb, conn, tid, monkeypatch):
    """Claim *tid* and record a live worker; returns the list worker terminations land in."""
    terminations = []
    monkeypatch.setattr(kb, "_worker_alive", lambda *a, **k: True)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: terminations.append(a) or {})
    assert kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
    conn.commit()
    return terminations


def test_refuses_a_running_card_with_a_live_worker(board, monkeypatch):
    kb, conn = board
    tid = kb.create_task(conn, title="live", assignee="w")
    terminations = _live_claim(kb, conn, tid, monkeypatch)
    with pytest.raises(kb.LiveClaimError):
        kb.archive_task(conn, tid, refuse_live_claim=True, reason="withdrawn", actor="orchestrator")
    assert kb.get_task(conn, tid).status == "running"
    assert terminations == []
    assert kb.list_comments(conn, tid) == []
    assert not _events(kb, conn, tid, "archived")


def test_refuses_a_claim_that_lands_after_the_callers_own_check(board, monkeypatch):
    """The interleaving a precheck outside the transaction cannot close."""
    kb, conn = board
    tid = kb.create_task(conn, title="race", assignee="w")
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert not kb._claim_is_live(row)  # the caller's check: nothing to protect yet
    terminations = _live_claim(kb, conn, tid, monkeypatch)  # the dispatcher claims in between
    with pytest.raises(kb.LiveClaimError):
        kb.archive_task(conn, tid, refuse_live_claim=True, reason="withdrawn", actor="orchestrator")
    assert kb.get_task(conn, tid).status == "running"
    assert terminations == []


def test_reason_is_atomic_with_the_transition(board, monkeypatch):
    kb, conn = board
    tid = kb.create_task(conn, title="audit", assignee="w")

    def boom(*a, **k):
        raise OSError("injected comment-store failure")

    monkeypatch.setattr(kb, "add_comment", boom)
    with pytest.raises(OSError):
        kb.archive_task(conn, tid, refuse_live_claim=True, reason="required audit reason", actor="orchestrator")
    assert kb.get_task(conn, tid).status != "archived"
    assert not _events(kb, conn, tid, "archived")


def test_archived_event_and_comment_carry_reason_and_actor(board):
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w")
    assert kb.archive_task(conn, tid, refuse_live_claim=True, reason="dup of t_y", actor="orchestrator") is True
    assert kb.get_task(conn, tid).status == "archived"
    (ev,) = _events(kb, conn, tid, "archived")
    assert ev.payload == {"reason": "dup of t_y", "actor": "orchestrator"}
    (comment,) = kb.list_comments(conn, tid)
    assert (comment.author, comment.body) == ("orchestrator", "Archived: dup of t_y")


def test_default_call_still_archives_a_live_claimed_card(board, monkeypatch):
    """CLI and dashboard callers pass no new keyword and keep stock behaviour."""
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w")
    terminations = _live_claim(kb, conn, tid, monkeypatch)
    assert kb.archive_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "archived"
    assert len(terminations) == 1
    (ev,) = _events(kb, conn, tid, "archived")
    assert ev.payload is None
