"""Chat decision receipts and inaction use the existing completion-gate path."""
import json
import sqlite3


def load_receipts():
    from tests.completion_gate_support import module
    return module("receipts")


def databases(tmp_path, *, source_id="1789000000.123456", card="t_ab12cd34"):
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_name TEXT, timestamp REAL
        )""")
        result = json.dumps({"success": True, "message_id": 4884, "mirrored": True})
        db.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                   ("source-session", "user", "decision", 1789000000.123456))
        db.execute("ALTER TABLE messages ADD COLUMN platform_message_id TEXT")
        db.execute("UPDATE messages SET platform_message_id=? WHERE role='user'", (source_id,))
        db.execute("INSERT INTO messages(session_id,role,content,tool_call_id,tool_name,timestamp) VALUES(?,?,?,?,?,?)",
                   ("source-session", "tool", result, "send-1", "send_message", 1789000001.0))
        db.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                   ("telegram-session", "assistant", f"Applied {card} for source {source_id}.", 1789000001.1))
    board = tmp_path / "kanban.db"
    with sqlite3.connect(board) as db:
        db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        db.execute("INSERT INTO tasks VALUES(?,?)", (card, "running"))
    return state, board


def source(**overrides):
    value = {
        "profile": "generalist", "platform": "slack", "channel_id": "C0BTEFMAAJX",
        "request_id": "1789000000.123456", "timestamp": 1789000000.123456,
        "internal": False,
    }
    value.update(overrides)
    return value


def messages(*, target="telegram:123456789", card="t_ab12cd34", source_id="1789000000.123456",
             success=True, mirrored=True, message_id=4884):
    text = f"Applied {card} for source {source_id}."
    return [
        action_message(),
        {"role": "assistant", "tool_calls": [{"id": "send-1", "function": {
            "name": "send_message", "arguments": json.dumps({"target": target, "message": text})}}]},
        {"role": "tool", "tool_call_id": "send-1", "tool_name": "send_message",
         "content": json.dumps({"success": success, "message_id": message_id, "mirrored": mirrored})},
    ]


def action_message(*, task_id="t_ab12cd34", tool_name="kanban_comment"):
    return {"role": "tool", "tool_name": tool_name,
            "content": json.dumps({"ok": True, "comment_id": 9, "task_id": task_id})}


def settings(state, board):
    return {
        "chat_source_channel": "C0BTEFMAAJX", "telegram_destination": "telegram:123456789",
        "telegram_session_id": "telegram-session", "state_db_path": str(state),
        "kanban_db_path": str(board),
    }


def test_chat_decision_requires_matching_transport_and_mirror(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    decision = "SESSION-WRAPUP-20260920-17: apply t_ab12cd34."
    cfg = settings(state, board)

    missing = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision, final_response="Applied t_ab12cd34.",
        messages=[action_message()], session_id="source-session", settings=cfg,
    )
    assert missing["verdict"] == "failed"
    assert missing["mismatch"] == "uncarded_commitment:missing_chat_telegram_receipt"
    assert missing["subtype"] == "missing_chat_telegram_receipt"
    assert missing["source_identity"] == {
        "profile": "generalist", "channel": "C0BTEFMAAJX", "request_id": "1789000000.123456",
        "timestamp": 1789000000.123456,
    }

    passed = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision, final_response="Applied t_ab12cd34.",
        messages=messages(), session_id="source-session", settings=cfg,
    )
    assert passed["verdict"] == "reproduced"
    assert passed["receipt"] == {"destination": "telegram:123456789", "message_id": "4884", "card_id": "t_ab12cd34"}


def test_wrong_or_partial_receipts_fail_closed(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    cfg = settings(state, board)
    decision = "DECISION-20260920-17: apply t_ab12cd34."
    cases = [
        (messages(target="telegram:999"), "wrong_destination"),
        (messages(card="t_ffffffff"), "wrong_card"),
        (messages(source_id="1789000000.999999"), "wrong_source"),
        (messages(mirrored=False), "mirror_missing"),
        (messages(success=False), "transport_missing"),
        (messages(message_id="fake"), "transport_missing"),
    ]
    for transcript, suffix in cases:
        with sqlite3.connect(state) as db:
            db.execute("UPDATE messages SET content=? WHERE tool_call_id='send-1'", (transcript[-1]["content"],))
        result = receipts.assess_chat_receipt(
            profile="generalist", source=source(), user_message=decision,
            final_response="Applied t_ab12cd34.", messages=transcript,
            session_id="source-session", settings=cfg,
        )
        assert result["verdict"] == "failed"
        assert result["mismatch"].endswith(suffix)

    with sqlite3.connect(state) as db:
        db.execute("DELETE FROM messages WHERE role='user'")
    absent_source = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision,
        final_response="Applied t_ab12cd34.", messages=messages(),
        session_id="source-session", settings=cfg,
    )
    assert absent_source["mismatch"].endswith("source_not_persisted")


def test_receipt_requires_the_exact_persisted_source_and_owner_action(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    cfg = settings(state, board)
    decision = "DECISION-20260920-17: apply t_ab12cd34."

    wrong_timestamp = receipts.assess_chat_receipt(
        profile="generalist", source=source(timestamp=1789000000.999999),
        user_message=decision, final_response="Applied t_ab12cd34.", messages=messages(),
        session_id="source-session", settings=cfg,
    )
    assert wrong_timestamp["mismatch"].endswith("source_not_persisted")

    unrelated = messages()
    unrelated[0] = action_message(task_id="t_ffffffff")
    wrong_action = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision,
        final_response="Applied t_ab12cd34.", messages=unrelated,
        session_id="source-session", settings=cfg,
    )
    assert wrong_action["mismatch"].endswith("action_unverified")


def test_non_obligations_and_explicit_deferrals_are_excluded(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    cfg = settings(state, board)
    cases = [
        (source(channel_id="OTHER"), "SESSION-WRAPUP-1: apply t_ab12cd34."),
        (source(profile="cody"), "SESSION-WRAPUP-1: apply t_ab12cd34."),
        (source(internal=True), "SESSION-WRAPUP-1: apply t_ab12cd34."),
        (source(), "SWITCHBOARD-ARCHIVE-20260920: store only."),
        (source(), "STATUS-REQUEST-20260920-01: what is the current state?"),
        (source(), "SESSION-WRAPUP-1 CANCELLED: t_ab12cd34 is superseded."),
        (source(closed_channel=True), "SESSION-WRAPUP-1: apply t_ab12cd34."),
    ]
    for origin, text in cases:
        assert receipts.assess_chat_receipt(
            profile=origin["profile"], source=origin, user_message=text, final_response="No action.",
            messages=[], session_id="source-session", settings=cfg,
        ) is None
    assert receipts.assess_chat_receipt(
        profile="generalist", source=source(),
        user_message="DECISION-1: should we apply t_ab12cd34?",
        final_response="Consultation only.", messages=[], session_id="source-session", settings=cfg,
    ) is None
    pending = receipts.assess_chat_receipt(
        profile="generalist", source=source(),
        user_message="SESSION-WRAPUP-1: QUIET-NIGHT-DEFERRED t_ab12cd34.",
        final_response="Deferred on t_ab12cd34.", messages=[], session_id="source-session", settings=cfg,
    )
    assert pending["verdict"] == "unverified"
    assert pending["mismatch"] == "uncarded_commitment:chat_telegram_receipt_pending_quiet_hours"


def test_inaction_without_action_or_needs_input_is_a_commitment_failure():
    receipts = load_receipts()
    result = receipts.assess_inaction(
        "There is nothing I can do between messages; the board says running but no worker is executing.", [])
    assert result["verdict"] == "failed"
    assert result["mismatch"] == "uncarded_commitment:unsupported_inaction"
    assert result["repair"] == "Take the action or escalate to Lars — no third state."

    successful_action = [{"role": "tool", "tool_name": "kanban_comment", "content": '{"ok": true, "comment_id": 7}'}]
    assert receipts.assess_inaction("I could not act before; I reclaimed it now.", successful_action) is None
    needs_input = [{"role": "tool", "tool_name": "kanban_block", "content": '{"ok": true, "kind": "needs_input"}'}]
    assert receipts.assess_inaction("I cannot proceed without Lars's decision.", needs_input) is None
    heartbeat = [{"role": "tool", "tool_name": "kanban_heartbeat", "content": '{"ok": true}'}]
    assert receipts.assess_inaction("There is nothing I can do.", heartbeat)["verdict"] == "failed"


def test_plugin_routes_receipt_failure_through_existing_block_and_replay_dedupes(tmp_path, monkeypatch):
    from hermes_cli import plugins
    state, board = databases(tmp_path)
    cfg = {
        "plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": {
            "enabled": True, "chat_receipts_enabled": True, "db_path": str(tmp_path / "gate.db"),
            **settings(state, board),
        }}}},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    assert loaded is not None
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda _: "generalist")
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: [])
    kwargs = dict(
        final_response="Applied t_ab12cd34.", session_id="source-session", task_id="task",
        source_identity=source(), user_message="SESSION-WRAPUP-20260920-17: apply t_ab12cd34.",
    )
    blocked = manager.invoke_hook("before_turn_end", turn_id="one", messages=[action_message()], **kwargs)
    assert blocked[0]["action"] == "block"
    assert "Send the missing authorized line" in blocked[0]["message"]
    receipt = manager.invoke_hook("before_turn_end", turn_id="two", messages=messages(), **kwargs)
    assert receipt == []
    event = loaded.Gate({"enabled": True, "db_path": str(tmp_path / "gate.db")}, extract=lambda _: []).events()[-1]
    assert event["claims"][0]["subtype"] == "missing_chat_telegram_receipt"
    assert event["claims"][0]["source_identity"]["request_id"] == source()["request_id"]
    # Compacted replay can use the durable exact-source receipt binding; it never resends.
    assert manager.invoke_hook("before_turn_end", turn_id="three", messages=[], **kwargs) == []
    with sqlite3.connect(board) as db:
        db.execute("UPDATE tasks SET status='archived' WHERE id='t_ab12cd34'")
    stale = manager.invoke_hook("before_turn_end", turn_id="four", messages=[], **kwargs)
    assert stale[0]["action"] == "block"
    assert "action_unverified" in stale[0]["message"]
