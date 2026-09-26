"""Source-bound Chat→Telegram receipts and actionable-inaction claims.

The checker is read-only: it never sends, mirrors, changes cards, or manufactures
permission. It accepts only native tool results already persisted in the source
session and an independently persisted destination-session mirror.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

_CARD = re.compile(r"\bt_[0-9a-f]{8}\b", re.I)
_ARCHIVE_ONLY = re.compile(r"^\s*(?:SWITCHBOARD-ARCHIVE|STATUS-REQUEST)-[A-Z0-9_-]+\b", re.I)
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
    """Return True/False for a completed lookup, None when evidence is unavailable."""
    try:
        with _open_readonly(path) as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (card_id,)).fetchone()
        return row is not None and row[0] not in {"archived", "cancelled"}
    except Exception:
        return None


def binding_current(settings, binding):
    """A durable receipt remains usable only for its configured live owner card."""
    return (isinstance(binding, dict)
            and binding.get("destination") == str(settings.get("telegram_destination") or "")
            and isinstance(binding.get("card_id"), str)
            and _card_exists(settings.get("kanban_db_path"), binding["card_id"]) is True)


def _persisted_tool_result(path, session_id, call_id, content, source_timestamp):
    try:
        with _open_readonly(path) as db:
            row = db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='tool' "
                "AND tool_call_id=? AND tool_name='send_message' AND timestamp>=? LIMIT 1",
                (session_id, call_id, float(source_timestamp or 0)),
            ).fetchone()
        return row is not None and row[0] == content
    except Exception:
        return None


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
        return None


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
        return None


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
                if str(fn.get("name") or "") != "send_message":
                    continue
                calls[str(call.get("id") or "")] = _json(fn.get("arguments"))
        elif item.get("role") == "tool" and str(item.get("tool_name") or "") == "send_message":
            results[str(item.get("tool_call_id") or "")] = item
    return calls, results


def _persisted_send_attempts(path, session_id, source_timestamp):
    """Recover native send calls/results after transcript compaction."""
    try:
        with _open_readonly(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "tool_calls" not in columns:
                return None
            assistant_rows = db.execute(
                "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' "
                "AND timestamp>=? AND tool_calls IS NOT NULL ORDER BY id",
                (session_id, float(source_timestamp or 0)),
            ).fetchall()
            result_rows = db.execute(
                "SELECT tool_call_id, content FROM messages WHERE session_id=? AND role='tool' "
                "AND tool_name='send_message' AND timestamp>=? ORDER BY id",
                (session_id, float(source_timestamp or 0)),
            ).fetchall()
    except Exception:
        return None
    calls = {}
    for (raw_calls,) in assistant_rows:
        try:
            parsed = json.loads(raw_calls) if isinstance(raw_calls, str) else raw_calls
        except (TypeError, ValueError):
            continue
        for call in parsed if isinstance(parsed, list) else ():
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            if str(fn.get("name") or "") == "send_message":
                calls[str(call.get("id") or "")] = _json(fn.get("arguments"))
    results = {
        str(call_id or ""): {
            "role": "tool", "tool_name": "send_message",
            "tool_call_id": str(call_id or ""), "content": content,
        }
        for call_id, content in result_rows
    }
    return calls, results


def _message_binds_source(message, request_id):
    """Require an exact ``source <request-id>`` token, not a substring collision."""
    if not isinstance(message, str) or not request_id:
        return False
    pattern = (
        r"(?i)(?<![A-Za-z0-9_.-])source\s+" + re.escape(request_id)
        + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
    )
    return re.search(pattern, message) is not None


def _succeeded(result):
    return result.get("ok") is True or result.get("success") is True


def _action_status(final_response, messages):
    """Return ``(claimed, verified_cards)`` for source-owned actions."""
    claimed = bool(isinstance(final_response, str)
                   and _ACTION_RESULT.search(final_response) and _CARD.search(final_response))
    verified_cards = set()
    for item in messages or ():
        if not isinstance(item, dict) or item.get("role") != "tool":
            continue
        name = str(item.get("tool_name") or "")
        result = _json(item.get("content"))
        if not _succeeded(result):
            continue
        if name in _ACTION_TOOLS:
            claimed = True
            result_cards = set(_CARD.findall(json.dumps(result, sort_keys=True)))
            verified_cards.update(result_cards)
    return claimed, verified_cards


def _assessment(identity, suffix="", *, verdict="failed", repair=None):
    mismatch = "uncarded_commitment:missing_chat_telegram_receipt"
    if suffix:
        mismatch += ":" + suffix
    return {
        "verdict": verdict, "mismatch": mismatch,
        "subtype": "missing_chat_telegram_receipt", "source_identity": identity,
        "repair": repair or "Send the missing authorized line or retain the exact boundary on the existing owner card.",
    }


def _reconcile(identity, suffix="evidence_unavailable"):
    return _assessment(
        identity, suffix, verdict="unverified",
        repair="Reconcile the persisted transport and mirror evidence; do not resend until absence is proven.",
    )


def _source_state(source, request_id):
    state = source.get("source_state")
    if not isinstance(state, dict) or str(state.get("request_id") or "") != request_id:
        return {}
    return state


def _valid_message_id(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0


def assess_chat_receipt(*, profile, source, user_message, final_response, messages, session_id, settings):
    """Return a synthetic gate assessment, or None when this source is out of scope."""
    if not isinstance(source, dict) or profile != "generalist":
        return None
    channel = str(source.get("channel_id") or "")
    request_id = str(source.get("request_id") or "")
    if (str(source.get("platform") or "") != "slack"
            or channel != str(settings.get("chat_source_channel") or "")
            or not request_id or source.get("internal") is True):
        return None
    text = user_message if isinstance(user_message, str) else ""
    if _ARCHIVE_ONLY.search(text):
        return None
    identity = _source_identity(profile, source)
    state = _source_state(source, request_id)
    if state.get("status") in {"closed", "cancelled", "superseded"}:
        return None
    source_cards = set(_CARD.findall(text))
    if state.get("status") == "deferred_quiet_hours":
        source_evidence = _source_persisted(
            settings.get("state_db_path"), session_id, request_id, source.get("timestamp"))
        owner = str(state.get("card_id") or "")
        owner_state = _card_exists(settings.get("kanban_db_path"), owner) if owner in source_cards else False
        if source_evidence is None or owner_state is None:
            return _reconcile(identity)
        if (source_evidence is not True or owner_state is not True
                or state.get("destination") != str(settings.get("telegram_destination") or "")):
            return _assessment(identity, "quiet_hours_owner_missing",
                               repair="Retain the exact deferred boundary on the source-bound owner card.")
        return {
            "verdict": "unverified",
            "mismatch": "uncarded_commitment:chat_telegram_receipt_pending_quiet_hours",
            "subtype": "missing_chat_telegram_receipt", "source_identity": identity,
        }
    acted, verified_action_cards = _action_status(final_response, messages)
    if not acted:
        return None
    if source_cards:
        action_cards = verified_action_cards & source_cards
    else:
        source_cards = set(verified_action_cards)
        action_cards = set(verified_action_cards)
    if not source_cards:
        return _reconcile(identity, "owner_card_unresolved")

    expected_target = str(settings.get("telegram_destination") or "")
    expected_platform, separator, expected_chat_id = expected_target.partition(":")
    if separator != ":" or expected_platform != "telegram" or not expected_chat_id:
        return _reconcile(identity, "destination_configuration_unavailable")
    state_path = settings.get("state_db_path")
    source_evidence = _source_persisted(state_path, session_id, request_id, source.get("timestamp"))
    if source_evidence is None:
        return _reconcile(identity)
    if source_evidence is not True:
        return _assessment(identity, "source_not_persisted")
    destination_session = str(settings.get("telegram_session_id") or "")
    board_path = settings.get("kanban_db_path")
    if not action_cards:
        return _assessment(identity, "action_unverified")
    calls, results = _send_attempts(messages)
    persisted_attempts = _persisted_send_attempts(state_path, session_id, source.get("timestamp"))
    if persisted_attempts is None and not calls:
        return _reconcile(identity)
    if persisted_attempts:
        persisted_calls, persisted_results = persisted_attempts
        calls = {**calls, **persisted_calls}
        results = {**results, **persisted_results}
    saw_target = saw_source = saw_card = saw_transport = saw_mirror = evidence_unknown = False
    for call_id, args in calls.items():
        if str(args.get("target") or "") != expected_target:
            continue
        saw_target = True
        message = args.get("message")
        if not _message_binds_source(message, request_id):
            continue
        saw_source = True
        candidate_cards = set(_CARD.findall(message)) & source_cards & action_cards
        card_states = {card: _card_exists(board_path, card) for card in candidate_cards}
        if any(card_state is None for card_state in card_states.values()):
            evidence_unknown = True
        cards = {card for card, card_state in card_states.items() if card_state is True}
        if not cards:
            continue
        saw_card = True
        item = results.get(call_id)
        content = item.get("content") if item else None
        result = _json(content)
        persisted = _persisted_tool_result(
            state_path, session_id, call_id, content, source.get("timestamp"))
        if persisted is None:
            evidence_unknown = True
            continue
        message_id = result.get("message_id")
        if (result.get("success") is not True or result.get("platform") != expected_platform
                or str(result.get("chat_id") or "") != expected_chat_id
                or not _valid_message_id(message_id) or persisted is not True):
            continue
        saw_transport = True
        mirror = _mirror_exists(state_path, destination_session, message, source.get("timestamp"))
        if mirror is None:
            evidence_unknown = True
            continue
        if result.get("mirrored") is not True and mirror is True:
            return _reconcile(identity, "mirror_evidence_conflict")
        if result.get("mirrored") is not True or mirror is not True:
            continue
        saw_mirror = True
        return {
            "verdict": "reproduced", "mismatch": "", "subtype": "missing_chat_telegram_receipt",
            "source_identity": identity,
            "receipt": {"destination": expected_target, "message_id": str(message_id),
                        "card_id": sorted(cards)[0]},
        }
    if saw_transport and not saw_mirror:
        if evidence_unknown:
            return _reconcile(identity)
        return _assessment(
            identity, "mirror_missing",
            repair="Repair the destination-session mirror only; transport succeeded, so do not resend.",
        )
    if evidence_unknown:
        return _reconcile(identity)
    if saw_card and not saw_transport:
        return _assessment(
            identity, "transport_missing",
            repair="Reconcile the transport attempt; do not resend until absence is proven.",
        )
    if saw_source and not saw_card:
        return _assessment(identity, "wrong_card")
    if saw_target and not saw_source:
        return _assessment(identity, "wrong_source")
    if calls and not saw_target:
        return _assessment(identity, "wrong_destination")
    return _assessment(identity)


def assess_inaction(final_response, messages, *, user_message=""):
    """Reject self-reported inaction unless this turn acted or opened needs_input."""
    if not isinstance(final_response, str) or not _INACTION.search(final_response):
        return None
    response_cards = set(_CARD.findall(final_response))
    obligation_cards = response_cards or set(_CARD.findall(str(user_message or "")))
    if not obligation_cards:
        return {
            "verdict": "failed", "mismatch": "uncarded_commitment:unsupported_inaction",
            "subtype": "unsupported_inaction",
            "repair": "Take the action or escalate to Lars — no third state.",
        }
    resolved_cards = set()
    for item in messages or ():
        if not isinstance(item, dict) or item.get("role") != "tool":
            continue
        name = str(item.get("tool_name") or "")
        result = _json(item.get("content"))
        if name not in _ACTION_TOOLS or not _succeeded(result):
            continue
        result_cards = set(_CARD.findall(json.dumps(result, sort_keys=True)))
        matching_cards = result_cards & obligation_cards
        if not matching_cards:
            continue
        if name != "kanban_block" or result.get("block_kind") == "needs_input":
            resolved_cards.update(matching_cards)
    if obligation_cards <= resolved_cards:
        return None
    return {
        "verdict": "failed", "mismatch": "uncarded_commitment:unsupported_inaction",
        "subtype": "unsupported_inaction",
        "repair": "Take the action or escalate to Lars — no third state.",
    }
