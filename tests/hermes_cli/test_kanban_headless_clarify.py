"""A dispatched headless clarification is undelivered, never user input."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_notify import add_notify_sub
from hermes_cli.cli_agent_setup_mixin import _single_query_clarify_callback
from tools.clarify_tool import clarify_tool


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("subscribed", [False, True])
def test_dispatched_clarify_reports_undelivered_own_rail(tmp_path, monkeypatch, batch, subscribed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Decision", assignee="owner")
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, tid, claimer="owner")
        add_notify_sub(conn, task_id=tid, platform="slack", chat_id="OTHER",
                       notifier_profile="coordinator")
        if subscribed:
            add_notify_sub(conn, task_id=tid, platform="slack", chat_id="OWN",
                           thread_id="thread-1", notifier_profile="owner")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    questions = [{"question": "Approve?", "choices": ["Yes", "No"]}] if batch else None
    result = json.loads(clarify_tool("Approve?", callback=_single_query_clarify_callback, questions=questions))
    assert "error" in result
    assert "user_response" not in result and "responses" not in result
    assert "not delivered" in result["error"]
    assert "OTHER" not in result["error"]
    if subscribed:
        assert "slack:OWN" in result["error"]
        assert "thread-1" in result["error"]
    else:
        assert "no notification rail" in result["error"]
        assert "owner" in result["error"]


def test_home_fallback_is_scoped_and_descendants_do_not_own_ask(tmp_path, monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    from hermes_cli.profiles import get_active_profile_name

    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    for label in ("A", "B", "A"):
        home = tmp_path / label
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        (home / "config.yaml").write_text(
            f"platforms:\n  slack:\n    home_channel:\n      platform: slack\n      chat_id: HOME_{label}\n")
        kb.init_db()
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="Decision", assignee=get_active_profile_name())
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        result = json.loads(clarify_tool("Approve?", callback=_single_query_clarify_callback))
        assert f"slack:HOME_{label}" in result["error"]
        assert f"HOME_{'B' if label == 'A' else 'A'}" not in result["error"]
        with non_dispatcher_owned_context():
            result = json.loads(clarify_tool("Approve?", callback=_single_query_clarify_callback))
        assert "error" not in result
        assert "user_response" in result
