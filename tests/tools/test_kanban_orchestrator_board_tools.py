"""kanban_archive / kanban_promote / kanban_unlink: orchestrator-only board tools.

Thin wrappers over ``kanban_db.archive_task`` / ``specify_triage_task`` /
``unlink_tasks``; these pin the gate (hidden from task workers, refused at
runtime for them), the happy paths, and the refusals.
"""
from __future__ import annotations

import json
import os

import pytest

_NEW_TOOLS = ("kanban_archive", "kanban_promote", "kanban_unlink", "kanban_reassign")


@pytest.fixture
def board(monkeypatch, tmp_path):
    """Isolated board; caller is an orchestrator (no HERMES_KANBAN_TASK)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
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


def _call(name, args):
    from tools import kanban_tools as kt
    handler = {"kanban_archive": kt._handle_archive, "kanban_promote": kt._handle_promote,
               "kanban_unlink": kt._handle_unlink, "kanban_reassign": kt._handle_reassign}[name]
    return json.loads(handler(args))


def _events(kb, conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# --- gate -------------------------------------------------------------------

def test_new_tools_use_the_orchestrator_gate():
    from tools import kanban_tools as kt
    from tools.registry import registry
    for name in _NEW_TOOLS:
        assert name in kt._ORCHESTRATOR_TOOLS
        assert registry._tools[name].check_fn is kt._check_kanban_orchestrator_mode


def test_gate_off_for_task_worker_and_on_for_orchestrator(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import kanban_tools as kt
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kt._check_kanban_orchestrator_mode() is True
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert kt._check_kanban_orchestrator_mode() is False
    # Lifecycle tools stay visible to that worker; only routing tools hide.
    assert kt._check_kanban_mode() is True


@pytest.mark.parametrize("name,args", [
    ("kanban_archive", {"task_id": "X", "reason": "withdrawn"}),
    ("kanban_promote", {"task_id": "X"}),
    ("kanban_reassign", {"task_id": "X", "assignee": "sophia", "reason": "handoff"}),
    ("kanban_unlink", {"parent_id": "P", "child_id": "X"}),
])
def test_worker_is_refused_at_runtime_and_nothing_changes(board, monkeypatch, name, args):
    kb, conn = board
    parent = kb.create_task(conn, title="p", assignee="w")
    tid = kb.create_task(conn, title="x", assignee="w", parents=[parent], triage=True)
    args = {k: {"X": tid, "P": parent}.get(v, v) for k, v in args.items()}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_some_worker")
    out = _call(name, args)
    assert "orchestrator-only" in out.get("error", "")
    assert kb.get_task(conn, tid).status == "triage"
    assert kb.parent_ids(conn, tid) == [parent]


@pytest.mark.parametrize("name,args", [
    ("kanban_archive", {"task_id": "t_x", "reason": "r"}),
    ("kanban_promote", {"task_id": "t_x"}),
    ("kanban_reassign", {"task_id": "t_x", "assignee": "sophia", "reason": "handoff"}),
    ("kanban_unlink", {"parent_id": "a", "child_id": "b"}),
])
def test_delegate_child_is_refused(board, monkeypatch, name, args):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kt, "_delegation_ctx",
                        lambda pred, default: pred == "is_delegated_child_process_context" or default)
    out = _call(name, args)
    assert "delegate_task child" in out.get("error", "")


# --- kanban_archive ---------------------------------------------------------

def test_archive_happy_path_records_reason_as_comment(board):
    kb, conn = board
    tid = kb.create_task(conn, title="withdrawn", assignee="w")
    out = _call("kanban_archive", {"task_id": tid, "reason": "Withdrawn by John"})
    assert out == {"ok": True, "task_id": tid, "status": "archived"}
    assert kb.get_task(conn, tid).status == "archived"
    comments = kb.list_comments(conn, tid)
    assert [(c.author, c.body) for c in comments] == [
        ("orchestrator", "Archived: Withdrawn by John")]
    assert _events(kb, conn, tid, "archived")


def test_archive_requires_reason(board):
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w")
    out = _call("kanban_archive", {"task_id": tid, "reason": "  "})
    assert "reason is required" in out["error"]
    assert kb.get_task(conn, tid).status != "archived"


def test_archive_refuses_running_task_with_live_claim(board, monkeypatch):
    kb, conn = board
    tid = kb.create_task(conn, title="busy", assignee="w")
    kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
    conn.commit()
    monkeypatch.setattr(kb, "_worker_alive", lambda pid, started_at=None: True)
    out = _call("kanban_archive", {"task_id": tid, "reason": "stop"})
    assert "live worker claim" in out["error"]
    assert "Nothing changed" in out["error"]
    assert kb.get_task(conn, tid).status == "running"
    assert kb.list_comments(conn, tid) == []


def test_archive_allows_running_task_whose_worker_is_gone(board, monkeypatch):
    kb, conn = board
    tid = kb.create_task(conn, title="orphan", assignee="w")
    kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (999999, tid))
    conn.commit()
    monkeypatch.setattr(kb, "_worker_alive", lambda pid, started_at=None: False)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: {"skipped": True})
    out = _call("kanban_archive", {"task_id": tid, "reason": "worker died"})
    assert out["ok"] is True
    assert kb.get_task(conn, tid).status == "archived"


def test_archive_unknown_and_already_archived(board):
    kb, conn = board
    assert "unknown task" in _call("kanban_archive", {"task_id": "t_nope", "reason": "r"})["error"]
    tid = kb.create_task(conn, title="t", assignee="w")
    kb.archive_task(conn, tid)
    out = _call("kanban_archive", {"task_id": tid, "reason": "again"})
    assert "already archived" in out["error"]
    assert kb.list_comments(conn, tid) == []


def test_archive_refuses_claim_landing_after_handler_read(board, monkeypatch):
    """A worker claims between the handler's read and the archive txn: still
    refused atomically, and no worker termination is attempted."""
    kb, conn = board
    tid = kb.create_task(conn, title="race", assignee="w")
    terminations = []
    monkeypatch.setattr(kb, "_worker_alive", lambda *a, **k: True)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker",
                        lambda *a, **k: terminations.append(a) or {})
    real_archive = kb.archive_task

    def claim_then_archive(c, task_id, **kw):
        assert kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
        conn.commit()
        return real_archive(c, task_id, **kw)

    monkeypatch.setattr(kb, "archive_task", claim_then_archive)
    out = _call("kanban_archive", {"task_id": tid, "reason": "withdrawn"})
    assert "live worker claim" in out["error"]
    assert kb.get_task(conn, tid).status == "running"
    assert terminations == []
    assert kb.list_comments(conn, tid) == []
    assert not _events(kb, conn, tid, "archived")


def test_archive_reason_is_atomic_with_transition(board, monkeypatch):
    """If recording the reason fails, the archive rolls back: no committed
    archive without its reason, and the error says nothing changed."""
    kb, conn = board
    tid = kb.create_task(conn, title="audit", assignee="w")

    def boom(*a, **k):
        raise OSError("injected comment-store failure")

    monkeypatch.setattr(kb, "add_comment", boom)
    out = _call("kanban_archive", {"task_id": tid, "reason": "required audit reason"})
    assert "error" in out
    assert kb.get_task(conn, tid).status != "archived"
    assert not _events(kb, conn, tid, "archived")


def test_archive_event_carries_reason_and_actor(board):
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w")
    assert _call("kanban_archive", {"task_id": tid, "reason": "dup of t_y"})["ok"] is True
    (ev,) = _events(kb, conn, tid, "archived")
    assert ev.payload == {"reason": "dup of t_y", "actor": "orchestrator"}


def test_archive_task_default_behaviour_unchanged(board, monkeypatch):
    """CLI/dashboard callers (no new kwargs) still archive a live-claimed card."""
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w")
    kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
    conn.commit()
    monkeypatch.setattr(kb, "_worker_alive", lambda *a, **k: True)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: {"probe": True})
    assert kb.archive_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "archived"
    (ev,) = _events(kb, conn, tid, "archived")
    assert ev.payload is None


# --- kanban_promote ---------------------------------------------------------

def test_promote_triage_to_todo_lands_ready_when_parent_free(board):
    kb, conn = board
    tid = kb.create_task(conn, title="triaged", assignee="w", triage=True)
    out = _call("kanban_promote", {"task_id": tid})
    # Parent gating decides: no parents -> the re-gate promotes it straight to ready.
    assert out == {"ok": True, "task_id": tid, "status": "ready"}
    assert _events(kb, conn, tid, "specified")


def test_promote_triage_to_todo_stays_todo_under_open_parent(board):
    kb, conn = board
    parent = kb.create_task(conn, title="p", assignee="w")
    tid = kb.create_task(conn, title="c", assignee="w", parents=[parent], triage=True)
    out = _call("kanban_promote", {"task_id": tid, "to": "todo"})
    assert out["ok"] is True and out["status"] == "todo"


def test_promote_to_ready_refuses_open_parent_without_mutation(board):
    kb, conn = board
    parent = kb.create_task(conn, title="p", assignee="w")
    tid = kb.create_task(conn, title="c", assignee="w", parents=[parent], triage=True)
    out = _call("kanban_promote", {"task_id": tid, "to": "ready"})
    assert "unsatisfied parent dependencies" in out["error"] and parent in out["error"]
    assert kb.get_task(conn, tid).status == "triage"


def test_promote_to_ready_happy_path(board):
    kb, conn = board
    parent = kb.create_task(conn, title="p", assignee="w")
    tid = kb.create_task(conn, title="c", assignee="w", parents=[parent], triage=True)
    kb.archive_task(conn, parent)
    out = _call("kanban_promote", {"task_id": tid, "to": "ready"})
    assert out == {"ok": True, "task_id": tid, "status": "ready"}


@pytest.mark.parametrize("status", ["todo", "ready", "blocked", "done"])
def test_promote_refuses_non_triage(board, status):
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w")
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    conn.commit()
    out = _call("kanban_promote", {"task_id": tid})
    assert "only applies to 'triage'" in out["error"]
    assert kb.get_task(conn, tid).status == status


def test_promote_rejects_bad_target_and_unknown_task(board):
    kb, conn = board
    tid = kb.create_task(conn, title="t", assignee="w", triage=True)
    assert "to must be" in _call("kanban_promote", {"task_id": tid, "to": "running"})["error"]
    assert kb.get_task(conn, tid).status == "triage"
    assert "unknown task" in _call("kanban_promote", {"task_id": "t_nope"})["error"]


# --- kanban_unlink ----------------------------------------------------------

def test_unlink_happy_path_regates_child_and_records_actor(board):
    kb, conn = board
    parent = kb.create_task(conn, title="p", assignee="w")
    child = kb.create_task(conn, title="c", assignee="w", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    out = _call("kanban_unlink", {"parent_id": parent, "child_id": child})
    assert out == {"ok": True, "parent_id": parent, "child_id": child, "status": "ready"}
    assert kb.parent_ids(conn, child) == []
    [ev] = _events(kb, conn, child, "unlinked")
    assert ev.payload == {"parent": parent, "child": child, "actor": "orchestrator"}


def test_unlink_missing_link_and_args(board):
    kb, conn = board
    a = kb.create_task(conn, title="a", assignee="w")
    b = kb.create_task(conn, title="b", assignee="w")
    assert "no link" in _call("kanban_unlink", {"parent_id": a, "child_id": b})["error"]
    assert "both parent_id and child_id" in _call("kanban_unlink", {"parent_id": a})["error"]


def test_unlink_db_actor_is_optional_for_existing_callers(board):
    kb, conn = board
    parent = kb.create_task(conn, title="p", assignee="w")
    child = kb.create_task(conn, title="c", assignee="w", parents=[parent])
    assert kb.unlink_tasks(conn, parent, child) is True
    [ev] = _events(kb, conn, child, "unlinked")
    assert ev.payload == {"parent": parent, "child": child}


# --- kanban_reassign --------------------------------------------------------

@pytest.fixture
def reassign_board(board, monkeypatch, tmp_path):
    from hermes_cli import profiles
    home = tmp_path / ".hermes"
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    for name in ("gurney", "sophia", "none", "unassign"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n")
    return board


def _snapshot(conn):
    """Compare all relevant rows AND the serialized SQLite image on refusal."""
    tables = ("tasks", "task_events", "task_comments", "task_runs", "task_links")
    return ({table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
             for table in tables}, conn.serialize())


def _reassign(tid, **overrides):
    from tools.registry import registry
    args = {"task_id": tid, "assignee": "sophia", "reason": "review PASS; return to owner"}
    args.update(overrides)
    return json.loads(registry.dispatch("kanban_reassign", args))


def test_reassign_blocked_preserves_history_and_only_changes_assignment(reassign_board):
    kb, conn = reassign_board
    parent = kb.create_task(conn, title="parent", assignee="gurney")
    tid = kb.create_task(conn, title="reviewed", assignee="sophia")
    assert kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="Implementation verified", reviewer="gurney")
    assert kb.claim_review_task(conn, tid)
    assert kb.block_task(conn, tid, reason="review PASS; handoff pending")
    kb.add_comment(conn, tid, "gurney", "Existing review evidence")
    child = kb.create_task(conn, title="child", assignee="gurney", parents=[tid])
    kb.link_tasks(conn, parent, tid)
    kb.set_model_override(conn, tid, "owner-model", "owner-provider")
    conn.execute("UPDATE tasks SET consecutive_failures=3, last_failure_error='old' WHERE id=?", (tid,))
    conn.commit()
    before, _ = _snapshot(conn)
    task_before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    out = _reassign(tid)
    assert out == {"ok": True, "task_id": tid, "assignee": "sophia",
                   "previous_assignee": "gurney", "status": "blocked"}
    task_after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert task_after == {**task_before, "assignee": "sophia", "consecutive_failures": 0,
                          "last_failure_error": None}
    after, _ = _snapshot(conn)
    assert after["task_runs"] == before["task_runs"]
    assert after["task_links"] == before["task_links"]
    assert after["task_comments"][:-1] == before["task_comments"]
    assert after["task_events"][:-2] == before["task_events"]
    [event] = _events(kb, conn, tid, "assigned")
    assert event.payload == {"assignee": "sophia", "from": "gurney", "actor": "orchestrator",
                             "reason": "review PASS; return to owner"}
    comment = kb.list_comments(conn, tid)[-1]
    assert (comment.author, comment.body) == (
        "orchestrator", "Reassigned gurney -> sophia: review PASS; return to owner")
    assert kb.parent_ids(conn, child) == [tid]


@pytest.mark.parametrize("status", ["triage", "todo", "ready", "blocked"])
def test_reassign_allowed_statuses_stay_unchanged(reassign_board, status):
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="card", assignee="gurney")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    conn.commit()
    assert _reassign(tid)["status"] == status
    assert kb.get_task(conn, tid).status == status


@pytest.mark.parametrize("case,changes,expected", [
    ("unknown", {}, "unknown task"),
    ("running", {}, "running"),
    ("review", {}, "review"),
    ("done", {}, "done"),
    ("archived", {}, "archived"),
    ("live_claim", {}, "claim"),
    ("empty_assignee", {"assignee": " "}, "assignee"),
    ("null_assignee", {"assignee": None}, "assignee"),
    ("none", {"assignee": "none"}, "assignee"),
    ("unassign", {"assignee": "unassign"}, "assignee"),
    ("unknown_profile", {"assignee": "not-a-profile"}, "profile"),
    ("invalid_profile", {"assignee": "../sophia"}, "profile"),
    ("same", {"assignee": "gurney"}, "already assigned"),
    ("empty_reason", {"reason": " "}, "reason"),
    ("null_reason", {"reason": None}, "reason"),
    ("unknown_arg", {"actor": "forged"}, "unknown parameter"),
])
def test_reassign_refusals_change_no_rows_or_database_bytes(reassign_board, monkeypatch, case, changes, expected):
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="card", assignee="gurney")
    if case in {"running", "review", "done", "archived"}:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (case, tid))
    if case == "live_claim":
        # A claim on a NON-running card must also be protected.
        conn.execute("UPDATE tasks SET status='blocked', claim_lock='claimed', worker_pid=? WHERE id=?",
                     (os.getpid(), tid))
        monkeypatch.setattr(kb, "_worker_alive", lambda *a, **k: True)
    conn.commit()
    before = _snapshot(conn)
    out = _reassign("t_missing" if case == "unknown" else tid, **changes)
    assert expected in out.get("error", ""), out
    assert out["error"].endswith("Nothing changed."), out
    assert _snapshot(conn) == before


@pytest.mark.parametrize("field", ["task_id", "assignee", "reason"])
def test_reassign_requires_string_parameters(reassign_board, field):
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="card", assignee="gurney")
    before = _snapshot(conn)
    out = _reassign(tid, **{field: 123}) if field != "task_id" else _reassign(123)
    assert field in out.get("error", "")
    assert out["error"].endswith("Nothing changed.")
    assert _snapshot(conn) == before


def test_reassign_refuses_claim_landing_at_transaction_boundary(reassign_board, monkeypatch):
    from contextlib import contextmanager
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="race", assignee="gurney")
    real_txn = kb.write_txn
    snapshot_after_claim = []

    @contextmanager
    def claim_then_begin(c, **kw):
        # Another writer lands a claim just BEFORE BEGIN IMMEDIATE.
        with real_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', claim_lock='interleaved', worker_pid=? WHERE id=?",
                         (os.getpid(), tid))
        snapshot_after_claim.append(_snapshot(conn))
        with real_txn(c, **kw):
            yield c

    monkeypatch.setattr(kb, "write_txn", claim_then_begin)
    out = _reassign(tid)
    assert "claim" in out.get("error", ""), out
    assert out["error"].endswith("Nothing changed.")
    assert snapshot_after_claim and _snapshot(conn) == snapshot_after_claim[0]


@pytest.mark.parametrize("failure", ["comment", "event"])
def test_reassign_provenance_failure_rolls_back_assignment(reassign_board, monkeypatch, failure):
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="atomic", assignee="gurney")
    before = _snapshot(conn)
    real_event = kb._append_event

    def boom(*a, **k):
        raise OSError("injected provenance failure")

    def fail_assigned(c, task_id, kind, *a, **k):
        if kind == "assigned":
            return boom()
        return real_event(c, task_id, kind, *a, **k)

    monkeypatch.setattr(kb, "add_comment" if failure == "comment" else "_append_event",
                        boom if failure == "comment" else fail_assigned)
    assert "injected provenance failure" in _reassign(tid)["error"]
    assert _snapshot(conn) == before


def test_reassign_explicit_board(reassign_board):
    kb, conn = reassign_board
    from hermes_cli import kanban_db_connect as kbc
    kb.init_db(board="other")
    with kbc.connect(board="other") as other:
        tid = kb.create_task(other, title="other board", assignee="gurney")
        assert _reassign(tid, board="other")["ok"]
        assert kb.get_task(other, tid).assignee == "sophia"
    assert kb.get_task(conn, tid) is None


def test_assign_task_defaults_keep_existing_behavior(reassign_board):
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="legacy", assignee="gurney")
    assert kb.assign_task(conn, tid, "gurney")  # Legacy no-op still records an event.
    assert kb.assign_task(conn, tid, None)      # CLI/dashboard can still unassign.
    assert kb.assign_task(conn, tid, "unknown-profile")
    assert [event.payload for event in _events(kb, conn, tid, "assigned")] == [
        {"assignee": "gurney", "from": "gurney"},
        {"assignee": None, "from": "gurney"},
        {"assignee": "unknown-profile", "from": None}]
    assert kb.list_comments(conn, tid) == []
    assert kb.claim_task(conn, tid)
    before = _snapshot(conn)
    with pytest.raises(RuntimeError, match="currently running"):
        kb.assign_task(conn, tid, "sophia")
    assert _snapshot(conn) == before


def test_reassign_registry_toolset_mcp_and_metrics_consistency():
    from pathlib import Path
    from tools import kanban_tools as kt
    from tools.registry import registry
    from toolsets import _HERMES_CORE_TOOLS
    from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
    assert "kanban_reassign" in kt._ORCHESTRATOR_TOOLS
    assert "kanban_reassign" in _HERMES_CORE_TOOLS
    assert "kanban_reassign" in EXPOSED_TOOLS
    schema = registry.get_schema("kanban_reassign")
    assert schema["parameters"]["required"] == ["task_id", "assignee", "reason"]
    assert set(schema["parameters"]["properties"]) == {"task_id", "assignee", "reason", "board"}
    schema_path = Path(__file__).resolve().parents[2] / "hermes_cli/observability/schemas/hermes.shared_metrics.v2.schema.json"
    metrics = json.loads(schema_path.read_text())
    # This checkout exports bounded tool categories, NOT raw tool-name enums.
    from hermes_cli.observability.relay_shared_metrics import _with_runtime_toolset
    from hermes_cli.observability.shared_metrics_contract import tool_category
    event = _with_runtime_toolset({"tool_name": "kanban_reassign"})
    assert event["toolset"] == "kanban"
    categories = metrics["$defs"]["tool_call_counter"]["properties"]["dimensions"]["properties"]["tool_category"]["enum"]
    assert tool_category(event) in categories


@pytest.mark.parametrize("context", ["worker", "delegate"])
def test_reassign_runtime_gate_keeps_database_identical(reassign_board, monkeypatch, context):
    from contextlib import nullcontext
    from agent.delegation_context import delegated_child_context
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="protected", assignee="gurney")
    before = _snapshot(conn)
    if context == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with delegated_child_context() if context == "delegate" else nullcontext():
        out = _reassign(tid)
    assert ("delegate_task child" if context == "delegate" else "orchestrator-only") in out["error"]
    assert _snapshot(conn) == before


@pytest.mark.parametrize("interleave", ["assignee", "status"])
def test_reassign_rechecks_handoff_snapshot_and_status_in_transaction(reassign_board, monkeypatch, interleave):
    from contextlib import contextmanager
    kb, conn = reassign_board
    tid = kb.create_task(conn, title="race", assignee="gurney")
    real_txn = kb.write_txn
    snapshots = []

    @contextmanager
    def change_then_begin(c, **kw):
        with real_txn(conn):
            if interleave == "assignee":
                conn.execute("UPDATE tasks SET assignee='default' WHERE id=?", (tid,))
            else:
                conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        snapshots.append(_snapshot(conn))
        with real_txn(c, **kw):
            yield c

    monkeypatch.setattr(kb, "write_txn", change_then_begin)
    out = _reassign(tid)
    assert ("assignment changed concurrently" if interleave == "assignee" else "review") in out["error"]
    assert out["error"].endswith("Nothing changed.")
    assert _snapshot(conn) == snapshots[0]


def test_reassign_notifies_only_after_provenance_commits(reassign_board, monkeypatch):
    kb, conn = reassign_board
    from hermes_cli import kanban_db_connect as kbc
    tid = kb.create_task(conn, title="notify", assignee="gurney")
    notifications = []

    def observe(c, task_id, fields):
        with kbc.connect() as observer:
            assert kb.get_task(observer, tid).assignee == "sophia"
            assert len(_events(kb, observer, tid, "assigned")) == 1
            assert len(kb.list_comments(observer, tid)) == 1
        notifications.append((task_id, fields))

    monkeypatch.setattr(kb, "notify_task_updated", observe)
    assert _reassign(tid)["ok"]
    assert notifications == [(tid, ("assignee",))]
