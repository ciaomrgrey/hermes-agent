"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
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




