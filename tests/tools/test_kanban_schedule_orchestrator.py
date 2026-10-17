"""Orchestrator-path guard for ``kanban_schedule``.

A caller with no ``HERMES_KANBAN_TASK`` may park only a ``blocked`` card whose
block is a time/dependency wait (dependency/transient, or the kernel re-kind of
a dependency wait with no open parent). Human/capability/unclassified holds and
non-blocked cards are refused with nothing changed; the persisted reason names
the acting profile. The worker path is covered in ``test_kanban_tools.py``.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def orch(monkeypatch, tmp_path):
    """Isolated board; orchestrator session for profile ``gabriel`` (no env task)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "gabriel")
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()


def _blocked(kb, *, kind, title="card", goal_mode=False):
    """Claim a fresh parentless card and block it the way a worker would."""
    with _conn() as conn:
        tid = kb.create_task(conn, title=title, assignee="emma",
                             goal_mode=goal_mode or None)
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="waiting on 03:00 cron output", kind=kind)
    return tid


def _snapshot(kb, tid):
    with _conn() as conn:
        t = kb.get_task(conn, tid)
        return (
            t.status, t.block_kind, t.claim_lock, t.current_run_id,
            [(e.kind, e.payload) for e in kb.list_events(conn, tid)],
            [(r.id, r.status, r.outcome, r.summary) for r in kb.list_runs(conn, tid)],
        )


def _schedule(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_schedule(args))


def test_rekinded_dependency_wait_is_scheduled_with_actor_and_unblockable(orch):
    kb = orch
    tid = _blocked(kb, kind="dependency")  # no open parent -> kernel re-kind
    with _conn() as conn:
        assert kb.get_task(conn, tid).block_kind == "needs_input"
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1].payload
    assert (blocked["requested_kind"], blocked["rekind_reason"]) == ("dependency", "no_open_parent")

    out = _schedule({"task_id": tid, "reason": "SCHEDULED_UNTIL=2026-10-18T03:30:00+02:00 cron"})

    expected = "[gabriel] SCHEDULED_UNTIL=2026-10-18T03:30:00+02:00 cron"
    assert out["ok"] is True and out["status"] == "scheduled" and out["reason"] == expected
    with _conn() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        event = [e for e in kb.list_events(conn, tid) if e.kind == "scheduled"][-1]
        assert event.payload == {"reason": expected}
        run = kb.latest_run(conn, tid)
        assert (run.outcome, run.summary) == ("scheduled", expected)
        assert event.run_id == run.id

    from tools import kanban_tools as kt
    unblocked = json.loads(kt._handle_unblock({"task_id": tid}))
    assert unblocked == {"ok": True, "task_id": tid, "status": "ready"}


def test_transient_block_is_schedulable(orch):
    kb = orch
    tid = _blocked(kb, kind="transient")
    out = _schedule({"task_id": tid, "reason": "retry after upstream outage window"})
    assert out["ok"] is True
    with _conn() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"


def _direct_blocked_no_event(kb):
    """Breaker-style park: blocked with no blocked event at all."""
    with _conn() as conn:
        tid = kb.create_task(conn, title="breaker", assignee="emma")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.commit()
    return tid


def _stale_transient_then_gave_up(kb):
    """Old transient block, unblocked, then re-parked by the breaker: the
    newest blocked event no longer describes the current hold."""
    tid = _blocked(kb, kind="transient")
    with _conn() as conn:
        assert kb.unblock_task(conn, tid)
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "gave_up", {"failures": 3, "sticky": True})
        conn.commit()
    return tid


@pytest.mark.parametrize("make, needle", [
    (lambda kb: _blocked(kb, kind="needs_input"), "'needs_input' is a human/capability hold"),
    (lambda kb: _blocked(kb, kind="capability"), "'capability' is a human/capability hold"),
    (_direct_blocked_no_event, "no blocked event"),
    (_stale_transient_then_gave_up, "stale"),
], ids=["genuine_needs_input", "capability", "payload_missing", "stale_episode"])
def test_holds_refused_and_unchanged(orch, make, needle):
    kb = orch
    tid = make(kb)
    before = _snapshot(kb, tid)
    out = _schedule({"task_id": tid, "reason": "try to park a hold"})
    assert "refused" in out["error"] and needle in out["error"]
    assert "Nothing changed" in out["error"]
    assert _snapshot(kb, tid) == before


def test_malformed_blocked_payload_refused(orch):
    kb = orch
    tid = _blocked(kb, kind="transient")
    with _conn() as conn:
        conn.execute(
            "UPDATE task_events SET payload='not json' WHERE task_id=? AND kind='blocked'", (tid,))
        conn.commit()
    before = _snapshot(kb, tid)
    out = _schedule({"task_id": tid, "reason": "park"})
    assert "carries no kind" in out["error"]
    assert _snapshot(kb, tid) == before


def test_payload_disagreeing_with_block_kind_refused(orch):
    kb = orch
    tid = _blocked(kb, kind="transient")
    with _conn() as conn:
        conn.execute("UPDATE tasks SET block_kind='capability' WHERE id=?", (tid,))
        conn.commit()
    before = _snapshot(kb, tid)
    out = _schedule({"task_id": tid, "reason": "park"})
    assert "disagrees" in out["error"]
    assert _snapshot(kb, tid) == before


def _ready(kb):
    with _conn() as conn:
        return kb.create_task(conn, title="ready", assignee="emma")


def _todo(kb):
    with _conn() as conn:
        parent = kb.create_task(conn, title="parent", assignee="emma")
        return kb.create_task(conn, title="child", assignee="emma", parents=[parent])


def _running(kb):
    with _conn() as conn:
        tid = kb.create_task(conn, title="live", assignee="emma")
        assert kb.claim_task(conn, tid) is not None
        assert kb.get_task(conn, tid).claim_lock
    return tid


@pytest.mark.parametrize("make, status", [
    (_ready, "ready"), (_todo, "todo"), (_running, "running"),
])
def test_non_blocked_sources_refused_and_unchanged(orch, make, status):
    kb = orch
    tid = make(kb)
    before = _snapshot(kb, tid)
    assert before[0] == status
    out = _schedule({"task_id": tid, "reason": "park"})
    assert f"is '{status}'" in out["error"] and "Nothing changed" in out["error"]
    assert _snapshot(kb, tid) == before


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_missing_reason_refused(orch, reason):
    kb = orch
    tid = _blocked(kb, kind="dependency")
    before = _snapshot(kb, tid)
    args = {"task_id": tid} if reason is None else {"task_id": tid, "reason": reason}
    out = _schedule(args)
    assert "reason is required" in out["error"]
    assert _snapshot(kb, tid) == before


def test_missing_task_id_refused(orch):
    kb = orch
    tid = _blocked(kb, kind="dependency")
    before = _snapshot(kb, tid)
    out = _schedule({"reason": "park"})
    assert "task_id is required" in out["error"]
    assert _snapshot(kb, tid) == before


def test_delegated_child_refused(orch, monkeypatch):
    kb = orch
    tid = _blocked(kb, kind="dependency")
    before = _snapshot(kb, tid)
    from tools import kanban_tools as kt
    monkeypatch.setattr(
        kt, "_delegation_ctx",
        lambda predicate, default: predicate in (
            "is_delegated_child_process_context", "is_delegated_child_context"),
    )
    out = _schedule({"task_id": tid, "reason": "park"})
    assert "delegate_task child agents are not Kanban run owners" in out["error"]
    assert _snapshot(kb, tid) == before


def test_worker_targeting_foreign_card_refused(orch, monkeypatch):
    kb = orch
    target = _blocked(kb, kind="dependency")
    with _conn() as conn:
        own = kb.create_task(conn, title="own", assignee="gabriel")
        kb.claim_task(conn, own)
        own_run = kb.get_task(conn, own).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(own_run))
    before = _snapshot(kb, target)
    out = _schedule({"task_id": target, "reason": "park"})
    assert "refusing to mutate" in out["error"]
    assert _snapshot(kb, target) == before


def test_goal_mode_refusal_retained(orch):
    kb = orch
    tid = _blocked(kb, kind="dependency", goal_mode=True)
    before = _snapshot(kb, tid)
    out = _schedule({"task_id": tid, "reason": "park"})
    assert "goal_mode tasks cannot be scheduled" in out["error"]
    assert _snapshot(kb, tid) == before


def test_worker_reason_stays_unprefixed(orch, monkeypatch):
    """Worker path: own running task, optional reason, no actor prefix."""
    kb = orch
    with _conn() as conn:
        own = kb.create_task(conn, title="own", assignee="gabriel")
        kb.claim_task(conn, own)
        run = kb.get_task(conn, own).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    out = _schedule({"reason": "SCHEDULED_UNTIL=x"})
    assert out["ok"] is True and out["reason"] == "SCHEDULED_UNTIL=x"


# --- Atomicity: authorization is bound to the mutation (Gurney P1) ---------
#
# A competing writer lands between the guard read and schedule_task's UPDATE
# (deterministically, by interleaving inside the patched schedule_task before
# it opens its transaction). The approved episode must no longer authorize.

def _interleave(kb, monkeypatch, competing):
    original = kb.schedule_task

    def wrapped(conn, task_id, **kwargs):
        with _conn() as other:
            competing(kb, other, task_id)
        return original(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "schedule_task", wrapped)


def _to_human_hold(kb, other, tid):
    assert kb.unblock_task(other, tid)
    assert kb.claim_task(other, tid)
    assert kb.block_task(other, tid, reason="Human approval required", kind="needs_input")


def _to_live_claim(kb, other, tid):
    assert kb.unblock_task(other, tid)
    assert kb.claim_task(other, tid)


def _to_ready(kb, other, tid):
    assert kb.unblock_task(other, tid)


def _to_new_transient_episode(kb, other, tid):
    # Same kind, but a new episode the orchestrator never inspected.
    assert kb.unblock_task(other, tid)
    assert kb.claim_task(other, tid)
    assert kb.block_task(other, tid, reason="new wait", kind="transient")


@pytest.mark.parametrize("competing", [
    _to_human_hold, _to_live_claim, _to_ready, _to_new_transient_episode,
], ids=["human_hold", "live_claim", "unblocked_ready", "new_episode"])
def test_concurrent_change_after_guard_is_refused(orch, monkeypatch, competing):
    kb = orch
    tid = _blocked(kb, kind="transient")
    _interleave(kb, monkeypatch, competing)
    out = _schedule({"task_id": tid, "reason": "park old transient wait"})
    assert "error" in out, out
    assert "changed after the hold check" in out["error"]
    assert "Nothing changed" in out["error"]
    with _conn() as conn:
        task = kb.get_task(conn, tid)
        assert task.status != "scheduled"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "scheduled"]
        assert not [r for r in kb.list_runs(conn, tid) if r.outcome == "scheduled"]
        if competing is _to_live_claim:
            assert task.status == "running" and task.claim_lock, "live claim must survive"
        if competing is _to_human_hold:
            assert (task.status, task.block_kind) == ("blocked", "needs_input")
        # No TEMP trigger leaks onto other connections (it is connection-local).
        assert not conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='trigger'").fetchall()


def test_concurrent_exact_snapshot_hold_replacement_refused(orch, monkeypatch):
    """Hold replaced by raw writes with no episode event: block_kind change alone refuses."""
    kb = orch
    tid = _blocked(kb, kind="dependency")  # rekinded needs_input
    def flip(kb, other, t):
        other.execute("UPDATE tasks SET block_kind='capability' WHERE id=?", (t,))
        other.commit()
    _interleave(kb, monkeypatch, flip)
    out = _schedule({"task_id": tid, "reason": "park"})
    assert "changed after the hold check" in out.get("error", "")
    with _conn() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_benign_concurrent_event_still_schedules(orch, monkeypatch):
    """A comment between guard and UPDATE does not change the episode."""
    kb = orch
    tid = _blocked(kb, kind="transient")
    def comment(kb, other, t):
        kb.add_comment(other, t, "gabriel", "still waiting on cron")
    _interleave(kb, monkeypatch, comment)
    out = _schedule({"task_id": tid, "reason": "park"})
    assert out.get("ok") is True and out["status"] == "scheduled"
    with _conn() as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='trigger'").fetchall()
