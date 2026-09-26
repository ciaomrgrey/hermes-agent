"""Chat decision receipts and inaction use the existing completion-gate path."""
import json
import sqlite3

import pytest


def load_receipts():
    from tests.completion_gate_support import module
    return module("receipts")


def databases(tmp_path, *, source_id="1789000000.123456", card="t_ab12cd34"):
    state = tmp_path / "state.db"
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_name TEXT, tool_calls TEXT, timestamp REAL
        )""")
        from tools.send_message_senders import _success
        native = _success("telegram", "123456789", message_id="4884")
        native["mirrored"] = True
        result = json.dumps(native)
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
             success=True, mirrored=True, message_id="4884"):
    from tools.send_message_senders import _success
    text = f"Applied {card} for source {source_id}."
    result = _success("telegram", "123456789", message_id=message_id) if success else {"success": False}
    result["mirrored"] = mirrored
    return [
        action_message(),
        {"role": "assistant", "tool_calls": [{"id": "send-1", "function": {
            "name": "send_message", "arguments": json.dumps({"target": target, "message": text})}}]},
        {"role": "tool", "tool_call_id": "send-1", "tool_name": "send_message",
         "content": json.dumps(result)},
    ]


def action_message(*, task_id="t_ab12cd34", tool_name="kanban_comment"):
    return {"role": "tool", "tool_name": tool_name,
            "content": json.dumps({"ok": True, "comment_id": 9, "task_id": task_id})}


def persist_result(state, transcript):
    with sqlite3.connect(state) as db:
        db.execute("UPDATE messages SET content=? WHERE tool_call_id='send-1'", (transcript[-1]["content"],))


def persist_call(state, transcript, *, include_result=True):
    with sqlite3.connect(state) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "tool_calls" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
        db.execute(
            "INSERT INTO messages(session_id,role,content,tool_calls,timestamp) VALUES(?,?,?,?,?)",
            ("source-session", "assistant", "", json.dumps(transcript[1]["tool_calls"]),
             source()["timestamp"] + 0.5),
        )
        if not include_result:
            db.execute("DELETE FROM messages WHERE tool_call_id='send-1'")


def persist_action(state, action=None):
    action = action or action_message()
    with sqlite3.connect(state) as db:
        db.execute(
            "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
            ("source-session", "tool", action["content"], action["tool_name"],
             source()["timestamp"] + 0.1),
        )


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
        (messages(success=False), "transport_missing"),
        (messages(message_id="fake"), "transport_missing"),
    ]
    for transcript, suffix in cases:
        persist_result(state, transcript)
        result = receipts.assess_chat_receipt(
            profile="generalist", source=source(), user_message=decision,
            final_response="Applied t_ab12cd34.", messages=transcript,
            session_id="source-session", settings=cfg,
        )
        assert result["verdict"] == "failed"
        assert result["mismatch"].endswith(suffix)
        if suffix == "transport_missing":
            assert "do not resend" in result["repair"].lower()

    with sqlite3.connect(state) as db:
        db.execute("DELETE FROM messages WHERE role='user'")
    absent_source = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision,
        final_response="Applied t_ab12cd34.", messages=messages(),
        session_id="source-session", settings=cfg,
    )
    assert absent_source["mismatch"].endswith("source_not_persisted")


def test_native_sender_string_message_id_is_accepted(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    transcript = messages(message_id="4884")
    persist_result(state, transcript)
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Lars decided: apply t_ab12cd34 now.",
        final_response="Applied t_ab12cd34.", messages=transcript,
        session_id="source-session", settings=settings(state, board),
    )
    assert result["verdict"] == "reproduced"
    assert result["receipt"]["message_id"] == "4884"

    wrong_native = messages(message_id="4884")
    payload = json.loads(wrong_native[-1]["content"])
    payload.update(platform="slack", chat_id="wrong")
    wrong_native[-1]["content"] = json.dumps(payload)
    persist_result(state, wrong_native)
    rejected = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Apply t_ab12cd34 now.",
        final_response="Applied t_ab12cd34.", messages=wrong_native,
        session_id="source-session", settings=settings(state, board),
    )
    assert rejected["verdict"] == "failed"
    assert rejected["mismatch"].endswith("transport_missing")


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


def test_receipt_rejects_non_native_tool_names_and_stale_persisted_results(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    cfg = settings(state, board)
    decision = "DECISION-20260920-17: apply t_ab12cd34."

    for forged_name in ("evil.send_message", "functions.send_message"):
        transcript = messages()
        transcript[1]["tool_calls"][0]["function"]["name"] = forged_name
        transcript[2]["tool_name"] = forged_name
        with sqlite3.connect(state) as db:
            db.execute("UPDATE messages SET tool_name=? WHERE tool_call_id='send-1'", (forged_name,))
        result = receipts.assess_chat_receipt(
            profile="generalist", source=source(), user_message=decision,
            final_response="Applied t_ab12cd34.", messages=transcript,
            session_id="source-session", settings=cfg,
        )
        assert result["verdict"] == "failed"
        assert result["mismatch"] == "uncarded_commitment:missing_chat_telegram_receipt"

    transcript = messages()
    with sqlite3.connect(state) as db:
        db.execute(
            "UPDATE messages SET tool_name='send_message', timestamp=? WHERE tool_call_id='send-1'",
            (source()["timestamp"] - 1,),
        )
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision,
        final_response="Applied t_ab12cd34.", messages=transcript,
        session_id="source-session", settings=cfg,
    )
    assert result["verdict"] == "failed"
    assert result["mismatch"].endswith("transport_missing")

    forged_action = messages()
    forged_action[0]["tool_name"] = "evil.kanban_comment"
    persist_result(state, forged_action)
    with sqlite3.connect(state) as db:
        db.execute(
            "UPDATE messages SET timestamp=? WHERE tool_call_id='send-1'",
            (source()["timestamp"] + 1,),
        )
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message=decision,
        final_response="Applied t_ab12cd34.", messages=forged_action,
        session_id="source-session", settings=cfg,
    )
    assert result["verdict"] == "failed"
    assert result["mismatch"].endswith("action_unverified")


def test_receipt_source_binding_rejects_prefix_collisions(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    transcript = messages(source_id=source()["request_id"] + "7")
    persist_result(state, transcript)
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(),
        user_message="DECISION-20260920-17: apply t_ab12cd34.",
        final_response="Applied t_ab12cd34.", messages=transcript,
        session_id="source-session", settings=settings(state, board),
    )
    assert result["verdict"] == "failed"
    assert result["mismatch"].endswith("wrong_source")


def test_receipt_card_must_match_the_card_verified_by_the_action(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    other = "t_bbbbbbbb"
    with sqlite3.connect(board) as db:
        db.execute("INSERT INTO tasks VALUES(?,?)", (other, "running"))
    transcript = messages(card=other)
    persist_result(state, transcript)
    with sqlite3.connect(state) as db:
        db.execute("DELETE FROM messages WHERE session_id='telegram-session'")
        db.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
            ("telegram-session", "assistant", transcript[1]["tool_calls"][0]["function"]["arguments"],
             source()["timestamp"] + 1),
        )
        message = json.loads(transcript[1]["tool_calls"][0]["function"]["arguments"])["message"]
        db.execute("UPDATE messages SET content=? WHERE session_id='telegram-session'", (message,))
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(),
        user_message="Apply t_ab12cd34 and t_bbbbbbbb now.",
        final_response="Applied t_ab12cd34.", messages=transcript,
        session_id="source-session", settings=settings(state, board),
    )
    assert result["verdict"] == "failed"
    assert result["mismatch"].endswith("wrong_card")


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
        (source(source_state={"request_id": source()["request_id"], "status": "cancelled"}),
         "SESSION-WRAPUP-1: apply t_ab12cd34."),
        (source(source_state={"request_id": source()["request_id"], "status": "closed"}),
         "SESSION-WRAPUP-1: apply t_ab12cd34."),
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
    pending_source = source(source_state={
        "request_id": source()["request_id"], "status": "deferred_quiet_hours",
        "card_id": "t_ab12cd34", "destination": "telegram:123456789",
    })
    pending = receipts.assess_chat_receipt(
        profile="generalist", source=source(),
        user_message="SESSION-WRAPUP-1: defer t_ab12cd34 until morning.",
        final_response="Deferred on t_ab12cd34.", messages=[], session_id="source-session", settings=cfg,
    )
    assert pending is None
    pending = receipts.assess_chat_receipt(
        profile="generalist", source=pending_source,
        user_message="SESSION-WRAPUP-1: defer t_ab12cd34 until morning.",
        final_response="Deferred on t_ab12cd34.", messages=[], session_id="source-session", settings=cfg,
    )
    assert pending["verdict"] == "unverified"
    assert pending["mismatch"] == "uncarded_commitment:chat_telegram_receipt_pending_quiet_hours"

    with sqlite3.connect(board) as db:
        db.execute("DELETE FROM tasks WHERE id='t_ab12cd34'")
    missing_owner = receipts.assess_chat_receipt(
        profile="generalist", source=pending_source,
        user_message="SESSION-WRAPUP-1: defer t_ab12cd34 until morning.",
        final_response="Deferred on t_ab12cd34.", messages=[], session_id="source-session", settings=cfg,
    )
    assert missing_owner["verdict"] == "failed"
    assert missing_owner["mismatch"].endswith("quiet_hours_owner_missing")


def test_markerless_action_qualifies_and_incidental_superseded_word_does_not_bypass(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(),
        user_message="The old plan is superseded; apply t_ab12cd34 now.",
        final_response="Applied t_ab12cd34.", messages=[action_message()],
        session_id="source-session", settings=settings(state, board),
    )
    assert result["verdict"] == "failed"
    assert result["mismatch"] == "uncarded_commitment:missing_chat_telegram_receipt"

    with sqlite3.connect(state) as db:
        db.execute(
            "INSERT INTO messages(session_id,role,content,platform_message_id,timestamp) "
            "VALUES(?,?,?,?,?)",
            ("source-session", "user", "later source", None, 1788999999.0),
        )
        later_action = action_message()
        db.execute(
            "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
            ("source-session", "tool", later_action["content"], later_action["tool_name"], 1789000002.1),
        )
    assert receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Should we change the plan?",
        final_response="Consultation only.", messages=[], session_id="source-session",
        settings=settings(state, board),
    ) is None
    assert receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Should we change the plan?",
        final_response="Consultation only.", messages=[later_action], session_id="source-session",
        settings=settings(state, board),
    ) is None


def test_inaction_without_action_or_needs_input_is_a_commitment_failure():
    receipts = load_receipts()
    result = receipts.assess_inaction(
        "There is nothing I can do between messages; the board says running but no worker is executing.", [])
    assert result["verdict"] == "failed"
    assert result["mismatch"] == "uncarded_commitment:unsupported_inaction"
    assert result["repair"] == "Take the action or escalate to Lars — no third state."

    successful_action = [action_message()]
    assert receipts.assess_inaction("I could not act before; I reclaimed it now.", successful_action,
                                    user_message="Reclaim t_ab12cd34") is None
    needs_input = [{"role": "tool", "tool_name": "kanban_block",
                    "content": '{"ok": true, "block_kind": "needs_input", "task_id": "t_ab12cd34"}'}]
    assert receipts.assess_inaction("I cannot proceed on t_ab12cd34 without Lars's decision.", needs_input,
                                    user_message="Decide t_ab12cd34 next step") is None
    heartbeat = [{"role": "tool", "tool_name": "kanban_heartbeat", "content": '{"ok": true}'}]
    assert receipts.assess_inaction("There is nothing I can do.", heartbeat)["verdict"] == "failed"

    unrelated = [action_message(task_id="t_ffffffff")]
    assert receipts.assess_inaction(
        "I cannot act on t_ab12cd34.", unrelated, user_message="Act on t_ab12cd34",
    )["verdict"] == "failed"
    assert receipts.assess_inaction(
        "There is nothing I can do.", [action_message()], user_message="Fix the unrelated issue",
    )["verdict"] == "failed"

    forged_action = [action_message(tool_name="evil.kanban_comment")]
    assert receipts.assess_inaction(
        "I cannot act on t_ab12cd34.", forged_action, user_message="Act on t_ab12cd34",
    )["verdict"] == "failed"
    forged_block = [{"role": "tool", "tool_name": "functions.kanban_block",
                     "content": '{"ok": true, "kind": "needs_input", "task_id": "t_ab12cd34"}'}]
    assert receipts.assess_inaction(
        "I cannot act on t_ab12cd34.", forged_block, user_message="Act on t_ab12cd34",
    )["verdict"] == "failed"

    action_on_other_card = [action_message(task_id="t_bbbbbbbb")]
    assert receipts.assess_inaction(
        "I cannot act on t_ab12cd34.", action_on_other_card,
        user_message="Act on t_ab12cd34 and t_bbbbbbbb",
    )["verdict"] == "failed"


def test_unavailable_evidence_is_unverified_and_never_instructs_resend(tmp_path, monkeypatch):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    cfg = settings(state, board)
    cfg["state_db_path"] = str(tmp_path / "missing.db")
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Apply t_ab12cd34 now.",
        final_response="Applied t_ab12cd34.", messages=[action_message()],
        session_id="source-session", settings=cfg,
    )
    assert result["verdict"] == "unverified"
    assert result["mismatch"].endswith("evidence_unavailable")
    assert "do not resend" in result["repair"].lower()

    compacted = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Apply t_ab12cd34 now.",
        final_response="All set.", messages=[], session_id="source-session", settings=cfg,
    )
    assert compacted["verdict"] == "unverified"
    assert compacted["mismatch"].endswith("evidence_unavailable")
    assert "do not resend" in compacted["repair"].lower()

    monkeypatch.setattr(receipts, "_open_readonly",
                        lambda _: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    locked = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Apply t_ab12cd34 now.",
        final_response="Applied t_ab12cd34.", messages=[action_message()],
        session_id="source-session", settings=settings(state, board),
    )
    assert locked["verdict"] == "unverified"
    assert locked["mismatch"].endswith("evidence_unavailable")


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


@pytest.mark.parametrize("final_response", ["Applied t_ab12cd34.", "All set."])
def test_plugin_recovers_fully_compacted_receipt_before_first_verdict(
        tmp_path, monkeypatch, final_response):
    from hermes_cli import plugins
    state, board = databases(tmp_path)
    transcript = messages()
    persist_action(state)
    persist_call(state, transcript)
    cfg = {"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": {
        "enabled": True, "chat_receipts_enabled": True, "db_path": str(tmp_path / "gate.db"),
        **settings(state, board),
    }}}}}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda _: "generalist")
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: [])

    result = manager.invoke_hook(
        "before_turn_end", final_response=final_response, session_id="source-session",
        task_id="task", turn_id="first", source_identity=source(),
        user_message="Lars decided: apply the approved change now.", messages=[],
    )
    assert result == []
    events = loaded.Gate(
        {"enabled": True, "db_path": str(tmp_path / "gate.db")}, extract=lambda _: [],
    ).events()
    assert events[-1]["claims"][0]["receipt"]["message_id"] == "4884"


@pytest.mark.parametrize("final_response", ["Applied t_ab12cd34.", "All set."])
def test_plugin_reconciles_persisted_ambiguous_send_without_resend(
        tmp_path, monkeypatch, final_response):
    from hermes_cli import plugins
    state, board = databases(tmp_path)
    transcript = messages()
    persist_action(state)
    persist_call(state, transcript, include_result=False)
    cfg = {"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": {
        "enabled": True, "chat_receipts_enabled": True, "db_path": str(tmp_path / "gate.db"),
        **settings(state, board),
    }}}}}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda _: "generalist")
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: [])

    result = manager.invoke_hook(
        "before_turn_end", final_response=final_response, session_id="source-session",
        task_id="task", turn_id="first", source_identity=source(),
        user_message="Lars decided: apply the approved change now.", messages=[],
    )
    assert result[0]["action"] == "block"
    assert "reconcile the transport attempt" in result[0]["message"].lower()
    assert "do not resend" in result[0]["message"].lower()
    assert "send the missing authorized line" not in result[0]["message"].lower()


def test_plugin_repairs_mirror_only_without_resend(tmp_path, monkeypatch):
    from hermes_cli import plugins
    state, board = databases(tmp_path)
    transcript = messages(mirrored=True)
    with sqlite3.connect(state) as db:
        db.execute("DELETE FROM messages WHERE session_id='telegram-session'")
    persist_result(state, transcript)
    cfg = {"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": {
        "enabled": True, "chat_receipts_enabled": True, "db_path": str(tmp_path / "gate.db"),
        **settings(state, board),
    }}}}}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda _: "generalist")
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: [])
    blocked = manager.invoke_hook(
        "before_turn_end", final_response="Applied t_ab12cd34.", session_id="source-session",
        task_id="task", turn_id="mirror", source_identity=source(),
        user_message="Apply t_ab12cd34 now.", messages=transcript,
    )
    assert blocked[0]["action"] == "block"
    assert "mirror only" in blocked[0]["message"].lower()
    assert "do not resend" in blocked[0]["message"].lower()


def test_contradictory_mirror_evidence_requires_reconciliation(tmp_path):
    receipts = load_receipts()
    state, board = databases(tmp_path)
    transcript = messages(mirrored=False)
    persist_result(state, transcript)
    result = receipts.assess_chat_receipt(
        profile="generalist", source=source(), user_message="Apply t_ab12cd34 now.",
        final_response="Applied t_ab12cd34.", messages=transcript,
        session_id="source-session", settings=settings(state, board),
    )
    assert result["verdict"] == "unverified"
    assert result["mismatch"].endswith("mirror_evidence_conflict")
    assert "do not resend" in result["repair"].lower()
