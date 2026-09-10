"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_STOP_NUDGE",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_for_genuine_unclosed_current_worker(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
        {
            "role": "tool",
            "name": "kanban_show",
            "content": (
                '{"task":{"status":"running","current_run_id":101},'
                '"runs":[{"id":101,"profile":"implementer","status":"running"}]}'
            ),
        },
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_when_own_run_requested_review(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="Review handoff", assignee="builder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None
        assert kb.request_review(conn, task_id, expected_run_id=run_id)
        reviewer = kb.claim_review_task(conn, task_id)
        assert reviewer is not None
        reviewer_run_id = reviewer.current_run_id
        assert reviewer_run_id is not None
        assert reviewer_run_id != run_id

        clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        assert build_kanban_stop_nudge(messages=[]) is None

        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(reviewer_run_id))
        assert build_kanban_stop_nudge(messages=[]) is not None


@pytest.mark.parametrize(
    ("transition", "outcome", "event_kind"),
    [
        ("complete", "completed", "completed"),
        ("block", "blocked", "blocked"),
        ("dependency", "blocked", "dependency_wait"),
    ],
)
def test_no_nudge_when_own_run_reached_terminal_outcome(
    clear_kanban_env, tmp_path, transition, outcome, event_kind,
):
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title=transition, assignee="builder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

        if transition == "complete":
            assert kb.complete_task(conn, task_id, summary="done", expected_run_id=run_id)
        else:
            kind = "dependency" if transition == "dependency" else None
            assert kb.block_task(
                conn, task_id, reason=transition, kind=kind, expected_run_id=run_id,
            )
        run = conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert run["outcome"] == outcome
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event["kind"] == event_kind

        clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_own_live_run_has_no_lifecycle_transition(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="Still running", assignee="builder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

        clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

        assert build_kanban_stop_nudge(messages=[]) is not None


def test_wrong_board_cannot_supply_terminal_run_state(clear_kanban_env, tmp_path):
    active_db = tmp_path / "active.db"
    wrong_db = tmp_path / "wrong.db"
    kbc.init_db(active_db)
    kbc.init_db(wrong_db)
    with kbc.connect_closing(active_db) as conn:
        task_id = kb.create_task(conn, title="Pinned board", assignee="builder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(wrong_db))
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    unrelated_terminal_result = [{
        "role": "tool",
        "name": "kanban_complete",
        "content": '{"ok":true,"status":"done"}',
    }]
    assert build_kanban_stop_nudge(messages=unrelated_terminal_result) is not None


def test_missing_board_fails_closed_without_creating_database(clear_kanban_env, tmp_path):
    missing_db = tmp_path / "missing" / "kanban.db"
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(missing_db))
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_missing")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "42")

    assert build_kanban_stop_nudge(messages=[]) is not None
    assert not missing_db.exists()


def test_malformed_run_id_fails_closed(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_invalid_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "not-an-integer")

    assert build_kanban_stop_nudge(messages=[]) is not None


def test_out_of_range_run_id_fails_closed(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="Invalid run", assignee="builder")
        assert kb.claim_task(conn, task_id) is not None

    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db_path))
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "9" * 100)

    assert build_kanban_stop_nudge(messages=[]) is not None


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_complete",
            "tool_call_id": "1",
            "content": '{"ok":true,"status":"done"}',
        },
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_kanban_request_review(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "kanban_request_review",
                        "arguments": '{"summary":"ready"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_request_review",
            "tool_call_id": "1",
            "content": '{"ok":true,"task":{"status":"review"}}',
        },
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_review_handoff_when_reviewer_is_already_running(clear_kanban_env):
    """A later reviewer claim does not revive the stale implementer's guard."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "review-call",
                    "type": "function",
                    "function": {
                        "name": "kanban_request_review",
                        "arguments": '{"summary":"ready"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "review-call",
            "content": '{"ok":true,"task_id":"t_review","status":"review"}',
        },
        {
            "role": "tool",
            "name": "kanban_show",
            "content": (
                '{"task":{"status":"running","current_run_id":202},'
                '"runs":[{"id":101,"outcome":"review_requested"},'
                '{"id":202,"profile":"reviewer","status":"running"}]}'
            ),
        },
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_kanban_request_changes(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "tool",
            "name": "kanban_request_changes",
            "tool_call_id": "1",
            "content": '{"ok":true,"task":{"status":"ready"}}',
        }
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_nudge_after_rejected_kanban_request_review(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "kanban_request_review",
                        "arguments": '{"summary":"ready"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "1",
            "content": '{"error":"goal judge rejected completion"}',
        },
    ]
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




