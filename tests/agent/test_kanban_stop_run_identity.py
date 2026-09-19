"""Turn-stop enforcement follows durable originating runs, not tool names."""
from types import SimpleNamespace

import pytest

from agent.turn_stop_gates import _kanban_stop_nudge
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    with kbc.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="Run-bound exit", assignee="builder")
        run = kb.claim_task(conn, tid)
        assert run is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
        yield conn, tid, run.current_run_id


@pytest.mark.parametrize("transition,successor", [
    ("review", False), ("changes", False), ("completed", False), ("blocked", False),
    ("review", True), ("changes", True), ("blocked", True),
])
def test_durable_handoff_stops_old_worker_without_touching_successor(board, monkeypatch, transition, successor):
    conn, tid, run_id = board
    if transition == "changes":
        assert kb.request_review(conn, tid, summary="ready", reviewer="reviewer", expected_run_id=run_id)
        review = kb.claim_review_task(conn, tid)
        assert review is not None
        run_id = review.current_run_id
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        assert kb.request_changes(conn, tid, reason="revise", expected_run_id=run_id)[0]
    elif transition == "review":
        assert kb.request_review(conn, tid, summary="ready", reviewer="reviewer", expected_run_id=run_id)
    elif transition == "completed":
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
    else:
        assert kb.block_task(conn, tid, reason="external dependency", expected_run_id=run_id)
    if successor:
        if transition == "review":
            assert kb.claim_review_task(conn, tid)
        else:
            if transition == "blocked":
                assert kb.unblock_task(conn, tid)
            assert kb.claim_task(conn, tid)
    before = list(conn.iterdump())
    # No transcript survives compression: persisted lifecycle remains authoritative.
    assert _kanban_stop_nudge(SimpleNamespace(), []) is None
    from agent import turn_stop_gates as gates

    monkeypatch.setattr(gates, "_verify_on_stop_nudge", lambda *_: None)
    monkeypatch.setattr(gates, "_pre_verify_nudge", lambda *_: None)
    messages = []
    verdict = gates.apply_stop_gates(
        SimpleNamespace(), {"role": "assistant", "content": "Handoff saved"},
        final_response="Handoff saved", messages=messages, conversation_history=[],
        pending_verification_response=None, pending_verification_response_previewed=False,
    )
    assert not verdict.continue_turn
    assert messages == []
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("tool", ["kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes"])
@pytest.mark.parametrize("identity", ["valid", "missing", "malformed", "superseded"])
def test_attempted_or_failed_tool_does_not_end_owned_run(board, monkeypatch, tool, identity):
    conn, tid, run_id = board
    if identity == "missing":
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    elif identity == "malformed":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-a-run")
    elif identity == "superseded":
        assert kb.reclaim_task(conn, tid)
        assert kb.claim_task(conn, tid)
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "x", "function": {"name": tool, "arguments": "{}"}}]},
        {"role": "tool", "name": tool, "tool_call_id": "x", "content": '{"error":"invalid transition"}'},
    ]
    before = list(conn.iterdump())
    nudge = _kanban_stop_nudge(SimpleNamespace(), messages)
    assert (nudge is None) == (identity == "superseded")
    assert _kanban_stop_nudge(SimpleNamespace(_kanban_stop_nudges=2), messages) is None
    assert list(conn.iterdump()) == before
