"""Receipt/inaction assessment and binding validation run on the hook's single clock.

Regression origin (t_9c6b8bb0 review R1, 27 Sep 2026): the adapter checked the
deadline only before/after the synchronous receipt helpers, and those helpers
opened ordinary SQLite connections (1 s busy wait, errors swallowed as
"evidence unavailable"). With a real contended state DB the native hook
abandoned the callback at its envelope and the timeout audit landed ~2 s later.

These tests hold REAL SQLite locks and go through native discovery plus
``PluginManager.invoke_hook`` with a scaled envelope (work clock 0.2 s,
framework envelope 1.0 s): the gate must return inside the envelope, never be
abandoned, and have its timeout outcome persisted BEFORE the hook returns.
"""
import json
import sqlite3
import sys
import time
from unittest.mock import patch

import pytest

from hermes_cli import plugins

WORK = 0.2
ENVELOPE = 1.0
CARD = "t_12345678"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    stamp = time.time()
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE messages(session_id, role, platform_message_id, timestamp)")
        db.execute("INSERT INTO messages VALUES(?,?,?,?)", ("s", "user", "request", stamp))
    board = tmp_path / "board.db"
    with sqlite3.connect(board) as db:
        db.execute("CREATE TABLE tasks(id, status)")
        db.execute("INSERT INTO tasks VALUES(?, 'running')", (CARD,))
    settings = {
        "enabled": True, "db_path": str(tmp_path / "gate.db"), "check_timeout_seconds": WORK,
        "chat_receipts_enabled": True, "inaction_enabled": True, "chat_source_channel": "fixture",
        "telegram_destination": "telegram:fixture", "state_db_path": str(state),
        "kanban_db_path": str(board),
    }
    cfg = {"plugins": {"enabled": ["completion-gate"],
                       "entries": {"completion-gate": {"settings": settings}}}}
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    assert loaded is not None
    # No provider, no transport: extraction is empty and escalation is forbidden.
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: [])
    monkeypatch.setattr(loaded, "send", lambda *a, **k: pytest.fail("transport forbidden"))
    source = {"platform": "slack", "channel_id": "fixture", "request_id": "request", "timestamp": stamp,
              "source_state": {"request_id": "request", "status": "deferred_quiet_hours",
                               "card_id": CARD, "destination": "telegram:fixture"}}
    return manager, loaded, settings, source


def _invoke(manager, source, *, final_response="Done"):
    with patch("hermes_constants.profile_name_for_home", return_value="generalist"), \
            patch.object(plugins, "_resolve_hook_callback_timeout", return_value=ENVELOPE):
        started = time.monotonic()
        manager.invoke_hook("before_turn_end", final_response=final_response, session_id="s",
                            turn_id="one", user_message=f"Lars decided: apply {CARD}; notify me.",
                            source_identity=source)
        return time.monotonic() - started


def _events(loaded, settings):
    return loaded.Gate(settings, extract=lambda _: []).events()


@pytest.mark.parametrize("contended", ["state_db_path", "kanban_db_path"])
def test_contended_evidence_db_times_out_inside_the_clock_without_abandonment(
        tmp_path, monkeypatch, contended):
    manager, loaded, settings, source = _load(tmp_path, monkeypatch)
    holder = sqlite3.connect(settings[contended], isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        elapsed = _invoke(manager, source)
        abandoned = bool(manager._hook_abandoned)
        # Read the audit while the lock is STILL held: it must already exist.
        events = _events(loaded, settings)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not abandoned
    assert elapsed < ENVELOPE, elapsed
    assert [e["action"] for e in events] == ["gate_error"]
    assert events[0]["diagnostics"]["cause"] == "aux_timeout"
    assert events[0]["diagnostics"]["exception_class"] == "TimeoutError"


def test_uncontended_control_delivers_promptly(tmp_path, monkeypatch):
    manager, loaded, settings, source = _load(tmp_path, monkeypatch)
    elapsed = _invoke(manager, source)
    assert not manager._hook_abandoned
    assert elapsed < WORK
    events = _events(loaded, settings)
    assert [e["action"] for e in events] == ["deliver"]
    claim, = events[0]["claims"]
    assert claim["artefact_kind"] == "chat_telegram_receipt"
    assert claim["mismatch"] == "uncarded_commitment:chat_telegram_receipt_pending_quiet_hours"


def test_receipts_disabled_policy_never_touches_the_contended_db(tmp_path, monkeypatch):
    manager, loaded, settings, source = _load(tmp_path, monkeypatch)
    entry = json.loads((tmp_path / "config.yaml").read_text())
    entry["plugins"]["entries"]["completion-gate"]["settings"]["chat_receipts_enabled"] = False
    (tmp_path / "config.yaml").write_text(json.dumps(entry))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: [])
    holder = sqlite3.connect(settings["state_db_path"], isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        elapsed = _invoke(manager, source)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not manager._hook_abandoned and elapsed < WORK
    assert [e["action"] for e in _events(loaded, settings)] == ["deliver"]


def _receipts(tmp_path, monkeypatch):
    manager, loaded, settings, source = _load(tmp_path, monkeypatch)
    return sys.modules[loaded.__name__ + ".receipts"], sys.modules[loaded.__name__ + ".gate"], settings


def test_binding_validation_expiry_propagates_instead_of_reading_as_absent(tmp_path, monkeypatch):
    receipts, gate, settings = _receipts(tmp_path, monkeypatch)
    binding = {"destination": "telegram:fixture", "card_id": CARD, "receipt_kind": "chat"}
    holder = sqlite3.connect(settings["kanban_db_path"], isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    token = gate._deadline.set(time.monotonic() + WORK)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            receipts.binding_current(settings, binding)
        assert time.monotonic() - started < WORK + 0.1
    finally:
        gate._deadline.reset(token)
        holder.execute("ROLLBACK")
        holder.close()


def test_evidence_wait_outside_an_evaluation_keeps_the_ordinary_unavailable_result(tmp_path, monkeypatch):
    """CLI/no-deadline callers keep the historical 1 s wait and None ("unknown")."""
    receipts, gate, settings = _receipts(tmp_path, monkeypatch)
    holder = sqlite3.connect(settings["kanban_db_path"], isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert receipts._card_exists(settings["kanban_db_path"], CARD) is None
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert receipts._card_exists(settings["kanban_db_path"], CARD) is True


def test_long_evidence_wait_is_capped_but_not_a_timeout_while_clock_remains(tmp_path, monkeypatch):
    receipts, gate, settings = _receipts(tmp_path, monkeypatch)
    monkeypatch.setattr(receipts, "_EVIDENCE_BUSY_SECONDS", 0.05)
    monkeypatch.setattr(receipts._EvidenceConnection, "statement_budget", 0.05)
    holder = sqlite3.connect(settings["kanban_db_path"], isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    token = gate._deadline.set(time.monotonic() + 5)
    try:
        started = time.monotonic()
        assert receipts._card_exists(settings["kanban_db_path"], CARD) is None
        assert time.monotonic() - started < 1
    finally:
        gate._deadline.reset(token)
        holder.execute("ROLLBACK")
        holder.close()


def test_transcript_traversal_stops_at_the_clock(tmp_path, monkeypatch):
    receipts, gate, _ = _receipts(tmp_path, monkeypatch)
    messages = [{"role": "tool", "tool_name": "kanban_comment", "content": "{}"}] * 1000
    token = gate._deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            receipts.assess_inaction(f"I could not finish {CARD}.", messages)
        with pytest.raises(TimeoutError):
            receipts._send_attempts(messages)
    finally:
        gate._deadline.reset(token)
    # Outside an evaluation the same traversal is unaffected.
    assert receipts.assess_inaction(f"I could not finish {CARD}.", messages)["subtype"] == "unsupported_inaction"
