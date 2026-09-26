"""Source-bound Chat→Telegram receipts and actionable-inaction claims.

The checker is read-only: it never sends, mirrors, changes cards, or manufactures
permission.  It accepts only native tool results already persisted in the source
session and an independently persisted destination-session mirror.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

_CARD = re.compile(r"\bt_[0-9a-f]{8}\b", re.I)
_DECISION = re.compile(r"\b(?:SESSION-WRAPUP|DECISION)-[A-Z0-9_-]+", re.I)
_EXCLUDED = re.compile(r"\b(?:SWITCHBOARD-ARCHIVE|STATUS-REQUEST|CANCELLED|SUPERSEDED)\b", re.I)
_QUIET_PENDING = re.compile(r"\bQUIET-NIGHT-DEFERRED\b", re.I)
_INACTION = re.compile(
    r"\b(?:did not|could not|cannot|can't|will not|won't|nothing I can do|no(?:thing)?\s*[-—:]?\s*not between messages|"
    r"board says running but no worker is executing|needs a permitted approval path)\b",
    re.I,
)
_ACTION_TOOLS = {
    "kanban_comment", "kanban_complete", "kanban_request_review", "kanban_request_changes",
    "kanban_block", "kanban_create", "kanban_unblock", "kanban_specify",
}
_ACTION_RESULT = re.compile(r"\b(?:applied|created|updated|filed|changed|implemented|completed|reclaimed)\b", re.I)


def _json(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_identity(profile, source):
    return {
        "profile": profile,
        "channel": str(source.get("channel_id") or ""),
        "request_id": str(source.get("request_id") or ""),
        "timestamp": source.get("timestamp"),
    }


def _open_readonly(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("receipt_database_unavailable")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    db.execute("PRAGMA query_only=ON")
    return db


def _card_exists(path, card_id):
    try:
        with _open_readonly(path) as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (card_id,)).fetchone()
        return row is not None and row[0] not in {"archived", "cancelled"}
    except Exception:
        return False


def binding_current(settings, binding):
    """A durable receipt remains usable only for its configured live owner card."""
    return (isinstance(binding, dict)
            and binding.get("destination") == str(settings.get("telegram_destination") or "")
            and isinstance(binding.get("card_id"), str)
            and _card_exists(settings.get("kanban_db_path"), binding["card_id"]))


def _persisted_tool_result(path, session_id, call_id, content):
    try:
        with _open_readonly(path) as db:
            row = db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_call_id=? ",
                (session_id, call_id),
            ).fetchone()
        return row is not None and row[0] == content
    except Exception:
        return False


def _source_persisted(path, session_id, request_id, timestamp):
    try:
        with _open_readonly(path) as db:
            row = db.execute(
                "SELECT 1 FROM messages WHERE session_id=? AND role='user' "
                "AND platform_message_id=? AND timestamp=? LIMIT 1",
                (session_id, request_id, timestamp),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mirror_exists(path, destination_session, message, source_timestamp):
    try:
        with _open_readonly(path) as db:
            row = db.execute(
                "SELECT 1 FROM messages WHERE session_id=? AND role='assistant' AND content=? "
                "AND timestamp>=? LIMIT 1",
                (destination_session, message, float(source_timestamp or 0)),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _send_attempts(messages):
    calls = {}
    results = {}
    for item in messages or ():
        if not isinstance(item, dict):
            continue
        if item.get("role") == "assistant":
            for call in item.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if str(fn.get("name") or "").rsplit(".", 1)[-1] != "send_message":
                    continue
                args = _json(fn.get("arguments"))
                calls[str(call.get("id") or "")] = args
        elif item.get("role") == "tool" and str(item.get("tool_name") or "").rsplit(".", 1)[-1] == "send_message":
            results[str(item.get("tool_call_id") or "")] = item
    return calls, results


def _succeeded(result):
    return result.get("ok") is True or result.get("success") is True


def _action_status(final_response, messages, source_cards):
    """Return ``(claimed, verified)`` for this source-owned action."""
    claimed = bool(isinstance(final_response, str)
                   and _ACTION_RESULT.search(final_response) and _CARD.search(final_response))
    for item in messages or ():
        if not isinstance(item, dict) or item.get("role") != "tool":
            continue
        name = str(item.get("tool_name") or "").rsplit(".", 1)[-1]
        result = _json(item.get("content"))
        if not _succeeded(result):
            continue
        if name in {"patch", "write_file"} and source_cards:
            return True, True
        if name in _ACTION_TOOLS:
            claimed = True
            result_cards = set(_CARD.findall(json.dumps(result, sort_keys=True)))
            if result_cards & source_cards:
                return True, True
    return claimed, False


def _failure(identity, suffix=""):
    mismatch = "uncarded_commitment:missing_chat_telegram_receipt"
    if suffix:
        mismatch += ":" + suffix
    return {
        "verdict": "failed", "mismatch": mismatch,
        "subtype": "missing_chat_telegram_receipt", "source_identity": identity,
        "repair": "Send the missing authorized line or retain the exact boundary on the existing owner card.",
    }


def assess_chat_receipt(*, profile, source, user_message, final_response, messages, session_id, settings):
    """Return a synthetic gate assessment, or None when this source is out of scope."""
    if not isinstance(source, dict) or profile != "generalist":
        return None
    channel = str(source.get("channel_id") or "")
    request_id = str(source.get("request_id") or "")
    if (str(source.get("platform") or "") != "slack"
            or channel != str(settings.get("chat_source_channel") or "")
            or not request_id or source.get("internal") is True or source.get("closed_channel") is True):
        return None
    text = user_message if isinstance(user_message, str) else ""
    if not _DECISION.search(text) or _EXCLUDED.search(text):
        return None
    identity = _source_identity(profile, source)
    if _QUIET_PENDING.search(text):
        return {
            "verdict": "unverified",
            "mismatch": "uncarded_commitment:chat_telegram_receipt_pending_quiet_hours",
            "subtype": "missing_chat_telegram_receipt", "source_identity": identity,
        }
    source_cards = set(_CARD.findall(text))
    acted, action_verified = _action_status(final_response, messages, source_cards)
    if not acted:
        return None

    expected_target = str(settings.get("telegram_destination") or "")
    state_path = settings.get("state_db_path")
    if not _source_persisted(state_path, session_id, request_id, source.get("timestamp")):
        return _failure(identity, "source_not_persisted")
    destination_session = str(settings.get("telegram_session_id") or "")
    board_path = settings.get("kanban_db_path")
    if not action_verified:
        return _failure(identity, "action_unverified")
    calls, results = _send_attempts(messages)
    saw_target = saw_source = saw_card = saw_transport = saw_mirror = False
    for call_id, args in calls.items():
        if str(args.get("target") or "") != expected_target:
            continue
        saw_target = True
        message = args.get("message")
        if not isinstance(message, str) or request_id not in message:
            continue
        saw_source = True
        cards = set(_CARD.findall(message)) & source_cards
        cards = {card for card in cards if _card_exists(board_path, card)}
        if not cards:
            continue
        saw_card = True
        item = results.get(call_id)
        content = item.get("content") if item else None
        result = _json(content)
        message_id = result.get("message_id")
        if (result.get("success") is not True or isinstance(message_id, bool)
                or not isinstance(message_id, int) or message_id <= 0
                or not _persisted_tool_result(state_path, session_id, call_id, content)):
            continue
        saw_transport = True
        if result.get("mirrored") is not True or not _mirror_exists(
                state_path, destination_session, message, source.get("timestamp")):
            continue
        saw_mirror = True
        return {
            "verdict": "reproduced", "mismatch": "", "subtype": "missing_chat_telegram_receipt",
            "source_identity": identity,
            "receipt": {"destination": expected_target, "message_id": str(message_id),
                        "card_id": sorted(cards)[0]},
        }
    if saw_transport and not saw_mirror:
        return _failure(identity, "mirror_missing")
    if saw_card and not saw_transport:
        return _failure(identity, "transport_missing")
    if saw_source and not saw_card:
        return _failure(identity, "wrong_card")
    if saw_target and not saw_source:
        return _failure(identity, "wrong_source")
    if calls and not saw_target:
        return _failure(identity, "wrong_destination")
    return _failure(identity)


def assess_inaction(final_response, messages):
    """Reject self-reported inaction unless this turn acted or opened needs_input."""
    if not isinstance(final_response, str) or not _INACTION.search(final_response):
        return None
    for item in messages or ():
        if not isinstance(item, dict) or item.get("role") != "tool":
            continue
        name = str(item.get("tool_name") or "").rsplit(".", 1)[-1]
        result = _json(item.get("content"))
        if name not in _ACTION_TOOLS or not _succeeded(result):
            continue
        if name != "kanban_block" or result.get("kind") == "needs_input":
            return None
    return {
        "verdict": "failed", "mismatch": "uncarded_commitment:unsupported_inaction",
        "subtype": "unsupported_inaction",
        "repair": "Take the action or escalate to Lars — no third state.",
    }
