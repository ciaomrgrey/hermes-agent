"""Supported CLI sends source-bound completion receipts without exposing a model tool."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE_ID = "1789000000.123456"
SOURCE_TS = 1789000000.123456
CARD = "t_ab12cd34"
DESTINATION = "telegram:123456789"
DESTINATION_SESSION = "telegram-session"
SOURCE_SESSION = "source-session"


def _state_db(path, *, source_rows=1, action_card=CARD, later_action=False,
              source_platform="slack", source_channel="C0BTEFMAAJX",
              source_text=None):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT)")
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (SOURCE_SESSION, source_platform, source_channel))
        db.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_name TEXT, tool_calls TEXT, timestamp REAL,
            platform_message_id TEXT
        )""")
        for _ in range(source_rows):
            db.execute(
                "INSERT INTO messages(session_id,role,content,timestamp,platform_message_id) VALUES(?,?,?,?,?)",
                (SOURCE_SESSION, "user", source_text or f"Apply {CARD}", SOURCE_TS, SOURCE_ID),
            )
        db.execute(
            "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
            (SOURCE_SESSION, "tool", json.dumps({"ok": True, "task_id": action_card}),
             "kanban_comment", SOURCE_TS + 0.1),
        )
        if later_action:
            db.execute(
                "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                (SOURCE_SESSION, "user", "later", SOURCE_TS + 1),
            )
            db.execute(
                "INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
                (SOURCE_SESSION, "tool", json.dumps({"ok": True, "task_id": CARD}),
                 "kanban_comment", SOURCE_TS + 1.1),
            )


def _native_state_db(path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=path)
    db.create_session(SOURCE_SESSION, "slack", chat_id="C0BTEFMAAJX")
    db.create_session(DESTINATION_SESSION, "telegram", chat_id="123456789")
    db.append_message(
        SOURCE_SESSION, "user", f"Apply {CARD}", timestamp=SOURCE_TS,
        platform_message_id=SOURCE_ID,
    )
    db.append_message(
        SOURCE_SESSION, "tool", json.dumps({"ok": True, "task_id": CARD}),
        tool_name="kanban_comment", timestamp=SOURCE_TS + 0.1,
    )
    db.close()


def _board_db(path, *, status="running"):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        db.execute("INSERT INTO tasks VALUES(?,?)", (CARD, status))


def _plugin(tmp_path, monkeypatch, *, source_rows=1, action_card=CARD, status="running",
            later_action=False, source_platform="slack", source_channel="C0BTEFMAAJX",
            source_text=None, native_state=False):
    from hermes_cli import plugins
    from tests.completion_gate_support import install

    state = tmp_path / "state.db"
    board = tmp_path / "kanban.db"
    gate = tmp_path / "gate.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if native_state:
        _native_state_db(state)
    else:
        _state_db(state, source_rows=source_rows, action_card=action_card, later_action=later_action,
                  source_platform=source_platform, source_channel=source_channel,
                  source_text=source_text)
    _board_db(board, status=status)
    cfg = {"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": {
        "enabled": True,
        "chat_receipts_enabled": True,
        "db_path": str(gate),
        "chat_source_channel": "C0BTEFMAAJX",
        "telegram_destination": DESTINATION,
        "telegram_session_id": DESTINATION_SESSION,
        "state_db_path": str(state),
        "kanban_db_path": str(board),
    }}}}}
    monkeypatch.setenv("HERMES_PROFILE", "generalist")
    monkeypatch.setenv("HERMES_SESSION_ID", SOURCE_SESSION)
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    cli = next(command for command in manager._cli_commands.values()
               if command["name"] == "completion-gate")
    parser = argparse.ArgumentParser()
    cli["setup_fn"](parser)
    return loaded, manager, cli, parser, state, board, gate


def _args(parser, message_file):
    return parser.parse_args([
        "receipt-send", "--source-request-id", SOURCE_ID,
        "--card", CARD, "--message-file", str(message_file),
    ])


def _mirror(state, message):
    with sqlite3.connect(state) as db:
        db.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
            (DESTINATION_SESSION, "assistant", message, SOURCE_TS + 0.5),
        )


def _receipt_rows(gate):
    with sqlite3.connect(gate) as db:
        return db.execute(
            "SELECT profile,session_id,source_channel,source_request_id,card_id,destination,"
            "message_hash,provider_message_id,mirror_disposition FROM completion_receipts ORDER BY id"
        ).fetchall()


def _enable_hook(loaded, manager, state, gate, monkeypatch):
    monkeypatch.setattr(loaded, "bounded_extract", lambda *_a, **_k: [])
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda _home: "generalist")

    def invoke(turn):
        return manager.invoke_hook(
            "before_turn_end", final_response=f"Applied {CARD}", session_id=SOURCE_SESSION,
            task_id="task", turn_id=turn, messages=[], user_message=f"Apply {CARD}",
            source_identity={"platform": "slack", "channel_id": "C0BTEFMAAJX",
                             "request_id": SOURCE_ID, "timestamp": SOURCE_TS, "internal": False},
        )

    return invoke


def test_receipt_send_uses_native_helper_persists_and_satisfies_restarted_hook(tmp_path, monkeypatch):
    loaded, manager, cli, parser, state, _board, gate = _plugin(tmp_path, monkeypatch)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    calls = []

    def native_send(args, **_kwargs):
        calls.append(args)
        _mirror(state, message)
        return json.dumps({
            "success": True, "platform": "telegram", "chat_id": "123456789",
            "message_id": "4884", "mirrored": True,
        })

    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", native_send)
    result = cli["handler_fn"](_args(parser, message_file))
    assert result["status"] == "mirrored"
    assert calls == [{"action": "send", "target": DESTINATION, "message": message}]
    rows = _receipt_rows(gate)
    assert rows[-1] == (
        "generalist", SOURCE_SESSION, "C0BTEFMAAJX", SOURCE_ID, CARD, DESTINATION,
        hashlib.sha256(message.encode()).hexdigest(), "4884", "mirrored",
    )

    # Retry after process/plugin reconstruction consumes the durable receipt and never sends twice.
    result = cli["handler_fn"](_args(parser, message_file))
    assert result["status"] == "mirrored"
    assert len(calls) == 1

    monkeypatch.setattr(loaded, "bounded_extract", lambda *_a, **_k: [])
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda _home: "generalist")
    assert manager.invoke_hook(
        "before_turn_end", final_response=f"Applied {CARD}", session_id=SOURCE_SESSION,
        task_id="task", turn_id="after-restart", messages=[], user_message=f"Apply {CARD}",
        source_identity={"platform": "slack", "channel_id": "C0BTEFMAAJX",
                         "request_id": SOURCE_ID, "timestamp": SOURCE_TS, "internal": False},
    ) == []
    event = loaded.Gate(
        {"enabled": True, "db_path": str(gate)}, extract=lambda _answer: [],
    ).events()[-1]
    assert event["claims"][0]["verdict"] == "reproduced"
    assert event["claims"][0]["receipt"]["message_id"] == "4884"

    # A prior gate event cannot outlive the exact destination mirror it certified.
    with sqlite3.connect(state) as db:
        db.execute("DELETE FROM messages WHERE session_id=?", (DESTINATION_SESSION,))
    blocked = manager.invoke_hook(
        "before_turn_end", final_response=f"Applied {CARD}", session_id=SOURCE_SESSION,
        task_id="task", turn_id="mirror-removed", messages=[], user_message=f"Apply {CARD}",
        source_identity={"platform": "slack", "channel_id": "C0BTEFMAAJX",
                         "request_id": SOURCE_ID, "timestamp": SOURCE_TS, "internal": False},
    )
    assert blocked[0]["action"] == "block"
    assert "missing_chat_telegram_receipt" in blocked[0]["message"]


@pytest.mark.parametrize(
    "change,error",
    [
        ("profile", "profile_scope_mismatch"),
        ("session", "session_scope_missing"),
        ("card", "current_card_mismatch"),
        ("relative", "message_file_must_be_absolute"),
    ],
)
def test_receipt_send_rejects_wrong_native_scope_before_transport(tmp_path, monkeypatch, change, error):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(tmp_path, monkeypatch)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    if change == "profile":
        monkeypatch.setenv("HERMES_PROFILE", "cody")
    elif change == "session":
        monkeypatch.delenv("HERMES_SESSION_ID")
    elif change == "card":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_ffffffff")
    calls = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: calls.append(args))
    args = _args(parser, message_file)
    if change == "relative":
        args.message_file = "message.txt"
    with pytest.raises(ValueError, match=error):
        cli["handler_fn"](args)
    assert calls == []


@pytest.mark.parametrize(
    "source_rows,action_card,status,error",
    [
        (0, CARD, "running", "source_row_missing"),
        (2, CARD, "running", "source_row_ambiguous"),
        (1, "t_ffffffff", "running", "source_turn_action_missing"),
        (1, CARD, "archived", "card_not_current"),
    ],
)
def test_receipt_send_requires_unique_source_current_card_and_same_turn_action(
        tmp_path, monkeypatch, source_rows, action_card, status, error):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(
        tmp_path, monkeypatch, source_rows=source_rows, action_card=action_card, status=status)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    calls = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: calls.append(args))
    with pytest.raises(ValueError, match=error):
        cli["handler_fn"](_args(parser, message_file))
    assert calls == []


def test_cross_session_duplicate_source_is_ambiguous(tmp_path, monkeypatch):
    _loaded, _manager, cli, parser, state, _board, _gate = _plugin(tmp_path, monkeypatch)
    with sqlite3.connect(state) as db:
        db.execute("INSERT INTO sessions VALUES(?,?,?)", ("other-session", "slack", "C0BTEFMAAJX"))
        db.execute(
            "INSERT INTO messages(session_id,role,content,timestamp,platform_message_id) VALUES(?,?,?,?,?)",
            ("other-session", "user", f"Apply {CARD}", SOURCE_TS, SOURCE_ID),
        )
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: sends.append(args))
    with pytest.raises(ValueError, match="source_row_ambiguous"):
        cli["handler_fn"](_args(parser, message_file))
    assert sends == []


def test_later_turn_action_cannot_authorize_the_source_receipt(tmp_path, monkeypatch):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(
        tmp_path, monkeypatch, action_card="t_ffffffff", later_action=True)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    calls = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: calls.append(args))
    with pytest.raises(ValueError, match="source_turn_action_missing"):
        cli["handler_fn"](_args(parser, message_file))
    assert calls == []


@pytest.mark.parametrize("message,error", [
    (f"Applied {CARD} for source 1789000000.999999.", "message_source_mismatch"),
    (f"Applied t_ffffffff for source {SOURCE_ID}.", "message_card_mismatch"),
])
def test_message_source_and_card_are_independent_predicates(
        tmp_path, monkeypatch, message, error):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(tmp_path, monkeypatch)
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    calls = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: calls.append(args))
    with pytest.raises(ValueError, match=error):
        cli["handler_fn"](_args(parser, message_file))
    assert calls == []


@pytest.mark.parametrize("message", [
    f"Applied {CARD} for source {SOURCE_ID}. MEDIA:/tmp/report.txt",
    f"[[as_document]] Applied {CARD} for source {SOURCE_ID}.",
])
def test_receipt_message_cannot_expand_into_native_attachments(tmp_path, monkeypatch, message):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(tmp_path, monkeypatch)
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: sends.append(args))
    with pytest.raises(ValueError, match="message_controls_forbidden"):
        cli["handler_fn"](_args(parser, message_file))
    assert sends == []


@pytest.mark.parametrize("platform,channel", [
    ("telegram", "C0BTEFMAAJX"),
    ("slack", "C_OTHER"),
])
def test_session_must_be_the_configured_slack_source(
        tmp_path, monkeypatch, platform, channel):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(
        tmp_path, monkeypatch, source_platform=platform, source_channel=channel)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: sends.append(args))
    with pytest.raises(ValueError, match="source_session_mismatch"):
        cli["handler_fn"](_args(parser, message_file))
    assert sends == []


def test_changed_message_for_same_source_is_reconciliation_not_a_second_send(tmp_path, monkeypatch):
    _loaded, _manager, cli, parser, state, _board, _gate = _plugin(tmp_path, monkeypatch)
    first_message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(first_message)
    sends = []
    import tools.send_message_tool as native

    def send(args):
        sends.append(args)
        _mirror(state, first_message)
        return json.dumps({"success": True, "platform": "telegram", "chat_id": "123456789",
                           "message_id": "4884", "mirrored": True})

    monkeypatch.setattr(native, "send_message_tool", send)
    assert cli["handler_fn"](_args(parser, message_file))["status"] == "mirrored"
    message_file.write_text(f"Different text for {CARD}, source {SOURCE_ID}.")
    assert cli["handler_fn"](_args(parser, message_file))["status"] == "reconciliation"
    assert len(sends) == 1


def test_transport_only_with_existing_exact_mirror_appends_mirrored_receipt(tmp_path, monkeypatch):
    loaded, _manager, cli, parser, state, board, gate = _plugin(tmp_path, monkeypatch)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: sends.append(args) or json.dumps({
        "success": True, "platform": "telegram", "chat_id": "123456789",
        "message_id": "4884", "mirrored": False,
    }))
    assert cli["handler_fn"](_args(parser, message_file))["status"] == "transport_only"
    _mirror(state, message)
    assert cli["handler_fn"](_args(parser, message_file))["status"] == "mirrored"
    assert len(sends) == 1
    assert [row[-1] for row in _receipt_rows(gate)] == ["reserved", "transport_only", "mirrored"]
    identity = {"profile": "generalist", "channel": "C0BTEFMAAJX",
                "request_id": SOURCE_ID, "timestamp": SOURCE_TS}
    receipt = loaded.receipts.durable_receipt_binding({
        "db_path": str(gate), "kanban_db_path": str(board), "state_db_path": str(state),
        "telegram_destination": DESTINATION, "telegram_session_id": DESTINATION_SESSION,
    }, identity, SOURCE_SESSION)
    assert receipt["message_id"] == "4884"


def test_transport_only_retry_repairs_mirror_without_resending(tmp_path, monkeypatch):
    _loaded, _manager, cli, parser, state, _board, gate = _plugin(tmp_path, monkeypatch)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: sends.append(args) or json.dumps({
        "success": True, "platform": "telegram", "chat_id": "123456789",
        "message_id": "4884", "mirrored": False,
    }))
    first = cli["handler_fn"](_args(parser, message_file))
    assert first["status"] == "transport_only"
    assert len(sends) == 1

    import gateway.mirror as mirror
    mirror_calls = []
    monkeypatch.setattr(
        mirror, "mirror_to_session",
        lambda *_a, **kwargs: mirror_calls.append(kwargs) or _mirror(state, message) or True,
    )
    second = cli["handler_fn"](_args(parser, message_file))
    assert second["status"] == "mirrored"
    assert len(sends) == 1
    assert mirror_calls == [{"source_label": "completion-gate", "session_id": DESTINATION_SESSION}]
    assert [row[-1] for row in _receipt_rows(gate)] == ["reserved", "transport_only", "mirrored"]


@pytest.mark.parametrize("native_result", [
    {"error": "timeout"},
    {"success": False, "error": "unavailable"},
    "not-json",
])
def test_ambiguous_or_unavailable_transport_is_durable_no_resend(
        tmp_path, monkeypatch, native_result):
    _loaded, _manager, cli, parser, _state, _board, gate = _plugin(tmp_path, monkeypatch)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool",
                        lambda args: sends.append(args) or (native_result if isinstance(native_result, str)
                                                           else json.dumps(native_result)))
    first = cli["handler_fn"](_args(parser, message_file))
    second = cli["handler_fn"](_args(parser, message_file))
    assert first["status"] == second["status"] == "reconciliation"
    assert len(sends) == 1
    assert [row[-1] for row in _receipt_rows(gate)] == ["reserved", "reconciliation"]


def test_receipt_binding_revalidates_profile_destination_card_session_and_exact_mirror(tmp_path, monkeypatch):
    loaded, _manager, cli, parser, state, board, gate = _plugin(tmp_path, monkeypatch)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda _args: (_mirror(state, message) or json.dumps({
        "success": True, "platform": "telegram", "chat_id": "123456789",
        "message_id": "4884", "mirrored": True,
    })))
    cli["handler_fn"](_args(parser, message_file))
    identity = {"profile": "generalist", "channel": "C0BTEFMAAJX",
                "request_id": SOURCE_ID, "timestamp": SOURCE_TS}
    cfg = {"db_path": str(gate), "kanban_db_path": str(board),
           "telegram_destination": DESTINATION, "telegram_session_id": DESTINATION_SESSION,
           "state_db_path": str(state)}
    receipt = loaded.receipts.durable_receipt_binding(cfg, identity, SOURCE_SESSION)
    assert receipt["message_id"] == "4884"
    for key, wrong in (("telegram_destination", "telegram:999"),
                       ("telegram_session_id", "other-session")):
        assert loaded.receipts.durable_receipt_binding({**cfg, key: wrong}, identity, SOURCE_SESSION) is None
    assert loaded.receipts.durable_receipt_binding(cfg, {**identity, "profile": "cody"}, SOURCE_SESSION) is None
    with sqlite3.connect(state) as db:
        db.execute("DELETE FROM messages WHERE session_id=?", (DESTINATION_SESSION,))
    assert loaded.receipts.durable_receipt_binding(cfg, identity, SOURCE_SESSION) is None
    with sqlite3.connect(board) as db:
        db.execute("UPDATE tasks SET status='archived' WHERE id=?", (CARD,))
    assert loaded.receipts.durable_receipt_binding(cfg, identity, SOURCE_SESSION) is None


@pytest.mark.parametrize("disposition", ["transport_only", "reconciliation"])
def test_actual_hook_preserves_cli_no_resend_disposition(tmp_path, monkeypatch, disposition):
    loaded, manager, cli, parser, state, _board, gate = _plugin(tmp_path, monkeypatch)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    sends = []
    import tools.send_message_tool as native

    def send(args):
        sends.append(args)
        if disposition == "reconciliation":
            return json.dumps({"error": "timeout"})
        return json.dumps({"success": True, "platform": "telegram", "chat_id": "123456789",
                           "message_id": "4884", "mirrored": False})

    monkeypatch.setattr(native, "send_message_tool", send)
    assert cli["handler_fn"](_args(parser, message_file))["status"] == disposition
    blocked = _enable_hook(loaded, manager, state, gate, monkeypatch)("partial-send")
    assert blocked and "do not resend" in blocked[0]["message"].lower()
    if disposition == "transport_only":
        assert "mirror" in blocked[0]["message"].lower()
    assert len(sends) == 1


def test_interrupted_reservation_tells_actual_hook_to_reconcile_without_resend(tmp_path, monkeypatch):
    loaded, manager, cli, parser, state, _board, gate = _plugin(tmp_path, monkeypatch)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    import tools.send_message_tool as native
    monkeypatch.setattr(
        native, "send_message_tool",
        lambda _args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        cli["handler_fn"](_args(parser, message_file))
    assert [row[-1] for row in _receipt_rows(gate)] == ["reserved"]
    blocked = _enable_hook(loaded, manager, state, gate, monkeypatch)("interrupted")
    assert blocked and "do not resend" in blocked[0]["message"].lower()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "channel", "action"])
def test_cached_receipt_revalidates_exact_source_and_action(tmp_path, monkeypatch, mutation):
    loaded, manager, cli, parser, state, _board, gate = _plugin(tmp_path, monkeypatch)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda _args: (_mirror(state, message) or json.dumps({
        "success": True, "platform": "telegram", "chat_id": "123456789",
        "message_id": "4884", "mirrored": True,
    })))
    assert cli["handler_fn"](_args(parser, message_file))["status"] == "mirrored"
    hook = _enable_hook(loaded, manager, state, gate, monkeypatch)
    assert hook("initial") == []

    with sqlite3.connect(state) as db:
        if mutation == "missing":
            db.execute("DELETE FROM messages WHERE platform_message_id=?", (SOURCE_ID,))
        elif mutation == "duplicate":
            db.execute(
                "INSERT INTO messages(session_id,role,content,timestamp,platform_message_id) VALUES(?,?,?,?,?)",
                (SOURCE_SESSION, "user", f"Apply {CARD}", SOURCE_TS, SOURCE_ID),
            )
        elif mutation == "channel":
            db.execute("UPDATE sessions SET chat_id='C_OTHER' WHERE id=?", (SOURCE_SESSION,))
        else:
            db.execute("DELETE FROM messages WHERE tool_name='kanban_comment'")
    blocked = hook(f"invalid-{mutation}")
    assert blocked and blocked[0]["action"] == "block"


@pytest.mark.parametrize("source_text", [
    f"SWITCHBOARD-ARCHIVE-TEST Archive only, no action: {CARD}",
    f"STATUS-REQUEST-TEST What is the state of {CARD}?",
    f"DECISION-TEST: should we apply {CARD}?",
    f"BOT-WAKE-TEST Internal wake for {CARD}",
])
def test_non_action_source_carriers_cannot_authorize_cli_send(tmp_path, monkeypatch, source_text):
    _loaded, _manager, cli, parser, _state, _board, _gate = _plugin(
        tmp_path, monkeypatch, source_text=source_text)
    message_file = tmp_path / "message.txt"
    message_file.write_text(f"Applied {CARD} for source {SOURCE_ID}.")
    sends = []
    import tools.send_message_tool as native
    monkeypatch.setattr(native, "send_message_tool", lambda args: sends.append(args))
    with pytest.raises(ValueError, match="source_not_actionable"):
        cli["handler_fn"](_args(parser, message_file))
    assert sends == []


def test_cli_uses_real_native_helper_and_mirror_with_only_transport_stubbed(tmp_path, monkeypatch):
    loaded, manager, cli, parser, state, _board, gate = _plugin(
        tmp_path, monkeypatch, native_state=True)
    message = f"Applied {CARD} for source {SOURCE_ID}."
    message_file = tmp_path / "message.txt"
    message_file.write_text(message)

    from hermes_state import SessionDB
    native_state = SessionDB(db_path=state)
    transport_calls = []
    import tools.send_message_tool as native
    import hermes_cli.send_cmd as send_cmd
    import gateway.config as gateway_config
    import gateway.mirror as mirror
    import hermes_state_registry

    config = SimpleNamespace(
        platforms={
            gateway_config.Platform.TELEGRAM: SimpleNamespace(
                enabled=True, token="fixture-token", extra={}),
        },
    )

    async def fake_transport(_token, chat_id, text, **_kwargs):
        transport_calls.append((str(chat_id), text))
        return {"success": True, "platform": "telegram", "chat_id": str(chat_id),
                "message_id": "4884"}

    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: config)
    monkeypatch.setattr(native, "_send_telegram", fake_transport)
    monkeypatch.setattr(mirror, "_find_session_id", lambda *_a, **_k: DESTINATION_SESSION)
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda: native_state)
    monkeypatch.setattr(hermes_state_registry, "release_or_close", lambda _db: None)

    result = cli["handler_fn"](_args(parser, message_file))
    assert result == {"status": "mirrored", "message_id": "4884"}
    assert transport_calls == [("123456789", message)]
    assert native_state.get_messages(DESTINATION_SESSION)[-1]["content"] == message
    assert _receipt_rows(gate)[-1][-1] == "mirrored"
    assert _enable_hook(loaded, manager, state, gate, monkeypatch)("native-helper") == []
    native_state.close()
