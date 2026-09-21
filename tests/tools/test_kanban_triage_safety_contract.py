"""Offline prerequisite probes for t_9a291d2b; no production candidate.

A schema-only wrapper must not claim native safety the kernel does not enforce.
Fixtures use only temporary boards and native lifecycle functions. The live
reproducer was read through kanban_show, never mutated or connected here.
"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    with kbc.connect_closing() as conn:
        yield conn


def triage_from_blocks(conn, kind, reason):
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(conn, title="Offline triage transition probe", assignee="builder")
    for index in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, kind=kind, reason=reason)
        if index < kb.BLOCK_RECURRENCE_LIMIT - 1:
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_current_unblock_refuses_reproducer_lifecycle_without_changes(board):
    from hermes_cli import kanban_db as kb
    from tools.registry import registry
    import tools.kanban_tools  # registers native handler

    tid = triage_from_blocks(board, "capability", "Offline t_8f685beb lifecycle analogue")
    before = list(board.iterdump())
    result = json.loads(registry.dispatch("kanban_unblock", {"task_id": tid}))
    assert "not blocked or unknown" in result["error"], result
    assert list(board.iterdump()) == before
    print("Native unblock refusal:", result)


@pytest.mark.parametrize("kind,reason", [
    ("needs_input", "Owner decision is still required"),
    ("capability", "Account permission denied; no bypass"),
    ("capability", "Security hold remains active"),
    ("capability", "Release requires independent acceptance"),
    ("capability", "PARK: explicit owner stop"),
])
def test_schema_only_specify_must_preserve_holds(board, kind, reason):
    from hermes_cli import kanban_db as kb

    tid = triage_from_blocks(board, kind, reason)
    before = list(board.iterdump())
    allowed = kb.specify_triage_task(board, tid, body="Updated implementation specification",
                                    assignee="other-builder", author="operator")
    task = kb.get_task(board, tid)
    specified = [e.payload for e in kb.list_events(board, tid) if e.kind == "specified"]
    assert not allowed and list(board.iterdump()) == before, (
        f"Unsafe schema-only exposure: allowed={allowed}, status={task.status}, "
        f"assignee={task.assignee}, block_kind={task.block_kind}, event={specified}"
    )


def test_native_specify_parent_gate_is_not_an_authorization_gate(board):
    from hermes_cli import kanban_db as kb

    parent = kb.create_task(board, title="Unfinished review prerequisite", assignee="reviewer", triage=True)
    tid = kb.create_task(board, title="Unheld specification", assignee="builder",
                         triage=True, parents=[parent])
    assert kb.specify_triage_task(board, tid, author="operator")
    assert kb.get_task(board, tid).status == "todo"
    allowed, reason = kb.promote_task(board, tid, actor="operator", dry_run=True)
    assert not allowed and "unsatisfied parent" in reason
    assert not kb.claim_task(board, tid)


def test_schema_only_specify_must_not_reassign_review_to_builder(board):
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(board, title="Review-owned work", assignee="builder")
    assert kb.request_review(board, tid, reviewer="reviewer", summary="Offline review fixture")
    for index in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.claim_review_task(board, tid)
        assert kb.block_task(board, tid, kind="capability", reason="Review prerequisite missing")
        if index < kb.BLOCK_RECURRENCE_LIMIT - 1:
            assert kb.unblock_task(board, tid)
            assert kb.get_task(board, tid).status == "review"
    assert kb.get_task(board, tid).status == "triage"
    allowed = kb.specify_triage_task(board, tid, assignee="builder", author="operator")
    task = kb.get_task(board, tid)
    assert not allowed, f"Reviewer reassigned to builder: allowed={allowed}, status={task.status}, assignee={task.assignee}"


def test_native_specify_records_changed_fields_not_actor(board):
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(board, title="Unheld specification", assignee="builder", triage=True)
    assert kb.specify_triage_task(board, tid, body="Bounded spec", author="operator")
    task = kb.get_task(board, tid)
    assert task.status == "ready"
    event = next(e for e in kb.list_events(board, tid) if e.kind == "specified")
    assert event.payload == {"changed_fields": ["body"]}
    print("Native specified event payload:", event.payload)
