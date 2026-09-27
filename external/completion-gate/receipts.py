"""Source-bound Chat→Telegram receipts and actionable-inaction claims.

The checker is read-only: it never sends, mirrors, changes cards, or manufactures
permission. It accepts only native tool results already persisted in the source
session and an independently persisted destination-session mirror.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from .gate import DeadlineConnection, _deadline

_CARD = re.compile(r"\bt_[0-9a-f]{8}\b", re.I)
# Latest estate policy: unsolicited receipts stay silent.  Authority to notify
# exists only when the WHOLE current request has one closed, conservative shape:
#   <carrier>: <imperative decision>; <bare solicitation>.
# The solicitation must be the entire final clause (so a prefix such as "do not,
# under any circumstances," cannot be split off), and the request as a whole must
# carry no quotation, negation, withdrawal, condition or silence wording.  Anything
# else -- including legitimate but unusual phrasing -- fails closed (no receipt).
_SOLICITATION = (
    r"(?:please\s+)?"
    r"(?:(?:send|give)\s+me\s+(?:a|the)\s+(?:receipt|confirmation)"
    r"|confirm\s+(?:back\s+)?to\s+me|report\s+back\s+to\s+me"
    r"|notify\s+me|tell\s+me|ping\s+me|message\s+me|let\s+me\s+know)"
    r"(?:\s+on\s+telegram)?"
    r"(?:\s+(?:when|once)\s+(?:it\s+is\s+|it's\s+)?(?:done|applied|complete))?")
# The decision side is a closed imperative, not free prose: one approved verb
# applied to explicit card ids (or "the approved change").  Reported,
# hypothetical, archived or rejected wording cannot match it.
_DECISION_TARGET = (
    r"(?:t_[0-9a-f]{8}(?:\s*(?:,\s*(?:and\s+)?|and\s+)t_[0-9a-f]{8})*"
    r"|the\s+approved\s+change)")
_DECISION = (
    r"(?:(?:apply|approve|accept|merge|ship|release|deploy|unblock|"
    r"proceed\s+with|go\s+ahead\s+with)\s+" + _DECISION_TARGET + r"(?:\s+now)?"
    r"|defer\s+" + _DECISION_TARGET + r"(?:\s+until\s+(?:morning|tomorrow))?)")
_SOLICITED_DECISION = re.compile(
    r"^(?:Lars\s+decided|DECISION-[A-Z0-9_-]+)\s*:\s*"
    r"(?P<decision>" + _DECISION + r")\s*[.;]\s*"
    r"(?P<ask>" + _SOLICITATION + r")\s*\.?$", re.I)
_REQUEST_VETO = re.compile(
    r"\b(?:no|not|never|none|nor|without|skip|stop|cancel\w*|withdr[ae]wn?|withdraw\w*|"
    r"revok\w*|retract\w*|rescind\w*|disregard|ignore|unless|except|if|earlier|previous\w*|"
    r"instead|silent\w*|quiet\w*|status|dont|don|won|can|shouldn|mustn)\b|n't\b", re.I)
_QUOTE_OR_APOSTROPHE_VARIANTS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u2032": "'", "\uff07": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00ab": '"', "\u00bb": '"', "\uff02": '"'})
_MAX_RECEIPT_SOURCE_AGE_SECONDS = 15 * 60
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


def _normalized_request(text):
    return unicodedata.normalize("NFKC", text).translate(_QUOTE_OR_APOSTROPHE_VARIANTS).strip()


def _receipt_requested(text):
    """True only when the whole request is a bare decision plus one bare solicitation.

    Authority is attached to the complete current request: quoted wording,
    negation, withdrawal, conditions or silence anywhere in it void authority.
    """
    request = _normalized_request(text)
    if any(ch in request for ch in "\"`?") or "\n" in request:
        return False
    if _REQUEST_VETO.search(request):
        return False
    return _SOLICITED_DECISION.fullmatch(request) is not None


def _source_is_actionable(text):
    """Admit only a Lars-owned decision that explicitly asks for a receipt.

    Carrier labels (``Lars decided:``, ``DECISION-*:``) prove provenance but grant
    no permission to notify; routine, status-only, silent, negated, quoted or
    withdrawn sources, and any source whose authorization is unproven, fail closed.
    """
    return isinstance(text, str) and _receipt_requested(text)


def _source_is_current(source_timestamp):
    """Bound receipt authority to a recent persisted source, not session ordering."""
    if isinstance(source_timestamp, bool) or not isinstance(source_timestamp, (int, float)):
        return False
    value = float(source_timestamp)
    if not math.isfinite(value):
        return False
    age = time.time() - value
    return -30 <= age <= _MAX_RECEIPT_SOURCE_AGE_SECONDS


def _source_is_receipt_eligible(text, source_timestamp):
    return _source_is_actionable(text) and _source_is_current(source_timestamp)


# Ordinary evidence reads keep their historical 1 s lock wait; inside a gate
# evaluation the hook's absolute deadline caps it further (never extends it).
_EVIDENCE_BUSY_SECONDS = 1.0


def _expired():
    deadline = _deadline.get()
    return deadline is not None and time.monotonic() >= deadline


def _check_deadline():
    """Raise on the hook's single clock; a no-op outside gate evaluation (CLI)."""
    if _expired():
        raise TimeoutError()


class _EvidenceConnection(DeadlineConnection):
    """Evidence reads wait at most min(1 s, time left on the shared clock)."""
    statement_budget = _EVIDENCE_BUSY_SECONDS


@contextmanager
def _open_readonly(path):
    """Read-only evidence connection bound to the gate's absolute deadline.

    Lock waits, statements and row traversal all stop at the deadline; expiry
    propagates as ``TimeoutError`` and is never reported as missing evidence.
    """
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("receipt_database_unavailable")
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError()
    # Outside a gate evaluation (CLI) keep SQLite's ordinary 1 s wait; inside
    # one the connection polls on the shared clock instead.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                         timeout=_EVIDENCE_BUSY_SECONDS if deadline is None else 0,
                         factory=_EvidenceConnection)
    try:
        if deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("PRAGMA query_only=ON")
        yield db
    except sqlite3.Error as exc:
        # A busy wait cut at the deadline or a progress-handler interrupt is
        # clock expiry, not unavailable evidence.
        if _expired():
            raise TimeoutError() from exc
        raise
    finally:
        db.close()


def _card_exists(path, card_id):
    """Return True/False for a completed lookup, None when evidence is unavailable."""
    try:
        with _open_readonly(path) as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (card_id,)).fetchone()
        return row is not None and row[0] not in {"archived", "cancelled"}
    except TimeoutError:
        raise
    except Exception:
        return None


def binding_current(settings, binding, *, source_identity=None, session_id=""):
    """A durable receipt remains usable only for its configured live owner card."""
    current = (isinstance(binding, dict)
               and binding.get("destination") == str(settings.get("telegram_destination") or "")
               and isinstance(binding.get("card_id"), str)
               and _card_exists(settings.get("kanban_db_path"), binding["card_id"]) is True)
    if not current or binding.get("receipt_kind") != "gate_owned":
        return current
    refreshed = durable_receipt_binding(settings, source_identity, session_id)
    return refreshed is not None and refreshed == binding


def _persisted_tool_result(path, session_id, call_id, content, source_timestamp):
    try:
        with _open_readonly(path) as db:
            row = db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='tool' "
                "AND tool_call_id=? AND tool_name='send_message' AND timestamp>=? LIMIT 1",
                (session_id, call_id, float(source_timestamp or 0)),
            ).fetchone()
        return row is not None and row[0] == content
    except TimeoutError:
        raise
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
    except TimeoutError:
        raise
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
    except TimeoutError:
        raise
    except Exception:
        return None


def _message_hash(message):
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _mirror_hash_exists(path, destination_session, message_hash, source_timestamp):
    """Verify an exact destination-session message without storing its body in the gate DB."""
    try:
        with _open_readonly(path) as db:
            rows = db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='assistant' "
                "AND timestamp>=? ORDER BY id",
                (destination_session, float(source_timestamp or 0)),
            ).fetchall()
        for row in rows:
            _check_deadline()
            if isinstance(row[0], str) and _message_hash(row[0]) == message_hash:
                return True
        return False
    except TimeoutError:
        raise
    except Exception:
        return None


def _receipt_connect(path):
    path = Path(path or "")
    if not path.is_absolute():
        raise ValueError("receipt_database_unavailable")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=1)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS completion_receipts (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, profile TEXT NOT NULL,
        session_id TEXT NOT NULL, source_channel TEXT NOT NULL,
        source_request_id TEXT NOT NULL, source_timestamp REAL NOT NULL,
        source_row_id INTEGER NOT NULL, card_id TEXT NOT NULL,
        destination TEXT NOT NULL, destination_session_id TEXT NOT NULL,
        message_hash TEXT NOT NULL, provider_message_id TEXT NOT NULL DEFAULT '',
        mirror_disposition TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS completion_receipts_source
        ON completion_receipts(profile,session_id,source_channel,source_request_id,source_timestamp)""")
    db.execute("""CREATE INDEX IF NOT EXISTS completion_receipts_stable_source
        ON completion_receipts(profile,source_channel,source_request_id)""")
    db.commit()
    return db


def _receipt_key(row):
    return tuple(row[key] for key in (
        "profile", "session_id", "source_channel", "source_request_id", "source_timestamp",
        "source_row_id", "card_id", "destination", "destination_session_id", "message_hash",
    ))


def _append_receipt(db, record, disposition, message_id=""):
    db.execute("""INSERT INTO completion_receipts (
        created,profile,session_id,source_channel,source_request_id,source_timestamp,
        source_row_id,card_id,destination,destination_session_id,message_hash,
        provider_message_id,mirror_disposition
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        time.time(), *_receipt_key(record), str(message_id or ""), disposition,
    ))


def _latest_source_receipt(db, record):
    return db.execute("""SELECT * FROM completion_receipts
        WHERE profile=? AND source_channel=? AND source_request_id=?
        ORDER BY id DESC LIMIT 1""", (
        record["profile"], record["source_channel"], record["source_request_id"],
    )).fetchone()


def _unique_source_row(path, session_id, request_id, source_channel):
    try:
        with _open_readonly(path) as db:
            rows = db.execute(
                "SELECT m.id,m.timestamp,m.session_id,s.source,s.chat_id,m.content FROM messages m "
                "JOIN sessions s ON s.id=m.session_id WHERE m.role='user' "
                "AND m.platform_message_id=? ORDER BY m.id LIMIT 2",
                (request_id,),
            ).fetchall()
    except TimeoutError:
        raise
    except Exception as exc:
        raise ValueError("source_database_unavailable") from exc
    if not rows:
        raise ValueError("source_row_missing")
    if len(rows) != 1:
        raise ValueError("source_row_ambiguous")
    row = rows[0]
    if row[2] != session_id or row[3] != "slack" or str(row[4] or "") != source_channel:
        raise ValueError("source_session_mismatch")
    return row


def _assessment_source_row(path, session_id, request_id, source_channel):
    """Read the exact source row; validate channel provenance when the native session table exists."""
    try:
        with _open_readonly(path) as db:
            rows = db.execute(
                "SELECT id,timestamp,session_id,content FROM messages WHERE role='user' "
                "AND platform_message_id=? ORDER BY id LIMIT 2",
                (request_id,),
            ).fetchall()
            has_sessions = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'",
            ).fetchone() is not None
            session_row = None
            if has_sessions and len(rows) == 1:
                session_row = db.execute(
                    "SELECT source,chat_id FROM sessions WHERE id=?", (rows[0][2],),
                ).fetchone()
    except TimeoutError:
        raise
    except Exception as exc:
        raise ValueError("source_database_unavailable") from exc
    if not rows:
        raise ValueError("source_row_missing")
    if len(rows) != 1:
        raise ValueError("source_row_ambiguous")
    row = rows[0]
    if row[2] != session_id:
        raise ValueError("source_session_mismatch")
    if has_sessions and (session_row is None or session_row[0] != "slack"
                         or str(session_row[1] or "") != source_channel):
        raise ValueError("source_session_mismatch")
    return row


def _receipt_configuration(settings):
    channel = str(settings.get("chat_source_channel") or "")
    destination = str(settings.get("telegram_destination") or "")
    destination_session = str(settings.get("telegram_session_id") or "")
    platform, separator, chat_id = destination.partition(":")
    if not channel:
        raise ValueError("source_channel_configuration_missing")
    if separator != ":" or platform != "telegram" or not chat_id:
        raise ValueError("destination_configuration_unavailable")
    if not destination_session:
        raise ValueError("destination_session_configuration_missing")
    return channel, destination, destination_session, platform, chat_id


def receipt_send(settings, *, source_request_id, card_id, message_file):
    """Send once through the supported native helper and persist source-bound gate evidence."""
    profile = str(os.environ.get("HERMES_PROFILE") or "")
    session_id = str(os.environ.get("HERMES_SESSION_ID") or "")
    current_card = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    if profile != "generalist":
        raise ValueError("profile_scope_mismatch")
    if not session_id:
        raise ValueError("session_scope_missing")
    if current_card != card_id:
        raise ValueError("current_card_mismatch")
    if not _CARD.fullmatch(str(card_id or "")):
        raise ValueError("invalid_card")
    path = Path(message_file)
    if not path.is_absolute():
        raise ValueError("message_file_must_be_absolute")
    try:
        message = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("message_file_unavailable") from exc
    if not message.strip():
        raise ValueError("message_file_empty")
    if (re.search(r"MEDIA:\s*(?:~/|/|[A-Za-z]:[/\\])", message)
            or "[[as_document]]" in message or "[[audio_as_voice]]" in message):
        raise ValueError("message_controls_forbidden")
    if not _message_binds_source(message, source_request_id):
        raise ValueError("message_source_mismatch")
    if card_id not in set(_CARD.findall(message)):
        raise ValueError("message_card_mismatch")

    channel, destination, destination_session, platform, chat_id = _receipt_configuration(settings)
    if _card_exists(settings.get("kanban_db_path"), card_id) is not True:
        raise ValueError("card_not_current")
    source_row = _unique_source_row(
        settings.get("state_db_path"), session_id, source_request_id, channel)
    source_row_id, source_timestamp, source_text = source_row[0], source_row[1], source_row[5]
    if not _source_is_actionable(source_text):
        raise ValueError("source_not_actionable")
    action = _persisted_action_cards(
        settings.get("state_db_path"), session_id, source_request_id, source_timestamp)
    if action is None or card_id not in action["cards"]:
        raise ValueError("source_turn_action_missing")

    record = {
        "profile": profile, "session_id": session_id, "source_channel": channel,
        "source_request_id": source_request_id, "source_timestamp": source_timestamp,
        "source_row_id": source_row_id, "card_id": card_id, "destination": destination,
        "destination_session_id": destination_session, "message_hash": _message_hash(message),
    }
    with _receipt_connect(settings.get("db_path")) as db:
        db.execute("BEGIN IMMEDIATE")
        prior = _latest_source_receipt(db, record)
        if prior is None:
            if not _source_is_current(source_timestamp) or not action["source_is_current"]:
                raise ValueError("source_not_current")
            _append_receipt(db, record, "reserved")
            prior_disposition = None
            message_id = ""
        elif _receipt_key(prior) != _receipt_key(record):
            db.commit()
            return {"status": "reconciliation", "message_id": ""}
        else:
            prior_disposition = prior["mirror_disposition"]
            message_id = prior["provider_message_id"]
        db.commit()

    if prior_disposition is not None:
        if prior_disposition in {"transport_only", "mirrored"}:
            mirror = _mirror_hash_exists(
                settings.get("state_db_path"), destination_session,
                record["message_hash"], source_timestamp)
            if mirror is True:
                if prior_disposition == "transport_only":
                    with _receipt_connect(settings.get("db_path")) as db:
                        _append_receipt(db, record, "mirrored", message_id)
                        db.commit()
                return {"status": "mirrored", "message_id": str(message_id)}
            try:
                from gateway.mirror import mirror_to_session
                mirrored = bool(mirror_to_session(
                    platform, chat_id, message, source_label="completion-gate",
                    session_id=destination_session))
            except Exception:
                mirrored = False
            if mirrored and _mirror_hash_exists(
                    settings.get("state_db_path"), destination_session,
                    record["message_hash"], source_timestamp) is True:
                with _receipt_connect(settings.get("db_path")) as db:
                    _append_receipt(db, record, "mirrored", message_id)
                    db.commit()
                return {"status": "mirrored", "message_id": str(message_id)}
            if prior_disposition == "mirrored":
                with _receipt_connect(settings.get("db_path")) as db:
                    _append_receipt(db, record, "transport_only", message_id)
                    db.commit()
                prior_disposition = "transport_only"
        status = prior_disposition if prior_disposition in {"mirrored", "transport_only"} else "reconciliation"
        return {"status": status,
                "message_id": str(message_id or "")}

    try:
        from hermes_cli.send_cmd import _load_hermes_env
        _load_hermes_env()
        from tools.send_message_tool import send_message_tool
        raw_result = send_message_tool({"action": "send", "target": destination, "message": message})
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        if not isinstance(result, dict):
            result = {}
    except Exception:
        result = {}
    message_id = result.get("message_id")
    transport_ok = (
        result.get("success") is True and result.get("platform") == platform
        and str(result.get("chat_id") or "") == chat_id and _valid_message_id(message_id)
    )
    disposition = "reconciliation"
    if transport_ok:
        mirror = _mirror_hash_exists(
            settings.get("state_db_path"), destination_session,
            record["message_hash"], source_timestamp)
        disposition = "mirrored" if result.get("mirrored") is True and mirror is True else "transport_only"
    with _receipt_connect(settings.get("db_path")) as db:
        _append_receipt(db, record, disposition, message_id if transport_ok else "")
        db.commit()
    return {"status": disposition, "message_id": str(message_id or "") if transport_ok else ""}


def _durable_receipt_row(settings, source_identity, session_id):
    """Return the latest gate-owned row for one exact persisted source identity."""
    if not isinstance(source_identity, dict):
        return None
    try:
        with _open_readonly(settings.get("db_path")) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT * FROM completion_receipts
                WHERE profile=? AND source_channel=? AND source_request_id=?
                ORDER BY id DESC LIMIT 1""", (
                source_identity.get("profile"), source_identity.get("channel"),
                source_identity.get("request_id"),
            )).fetchone()
    except TimeoutError:
        raise
    except Exception:
        return None
    return row


def _durable_receipt_attempt(settings, source_identity, session_id):
    """Return durable evidence that native transport may already have been attempted."""
    row = _durable_receipt_row(settings, source_identity, session_id)
    if row is None or row["mirror_disposition"] not in {
            "reserved", "transport_only", "reconciliation", "mirrored"}:
        return None
    return {
        "disposition": row["mirror_disposition"],
        "destination": row["destination"],
        "message_id": row["provider_message_id"],
        "card_id": row["card_id"],
        "receipt_kind": "gate_owned",
    }


def _durable_receipt_state(settings, source_identity, session_id):
    """Revalidate the latest gate receipt against its exact live source and destination."""
    row = _durable_receipt_row(settings, source_identity, session_id)
    if row is None:
        return None
    try:
        source_row = _unique_source_row(
            settings.get("state_db_path"), session_id, row["source_request_id"],
            row["source_channel"])
    except ValueError:
        return None
    if (source_row[0] != row["source_row_id"]
            or source_row[1] != row["source_timestamp"]
            or not _source_is_receipt_eligible(source_row[5], source_row[1])):
        return None
    action = _persisted_action_cards(
        settings.get("state_db_path"), session_id,
        row["source_request_id"], row["source_timestamp"])
    if action is None or row["card_id"] not in action["cards"]:
        return None
    if (row["destination"] != str(settings.get("telegram_destination") or "")
            or row["destination_session_id"] != str(settings.get("telegram_session_id") or "")
            or _card_exists(settings.get("kanban_db_path"), row["card_id"]) is not True):
        return None

    disposition = row["mirror_disposition"]
    if disposition in {"transport_only", "mirrored"}:
        if not _valid_message_id(row["provider_message_id"]):
            return None
        mirror = _mirror_hash_exists(
            settings.get("state_db_path"), row["destination_session_id"],
            row["message_hash"], row["source_timestamp"])
        if mirror is None:
            disposition = "reconciliation"
        elif mirror is True:
            disposition = "mirrored"
        else:
            disposition = "transport_only"
    elif disposition == "reserved":
        disposition = "reconciliation"
    return {
        "disposition": disposition,
        "destination": row["destination"],
        "message_id": row["provider_message_id"],
        "card_id": row["card_id"],
        "receipt_kind": "gate_owned",
    }


def durable_receipt_binding(settings, source_identity, session_id):
    """Return a currently valid CLI-owned mirrored receipt for an exact source identity."""
    state = _durable_receipt_state(settings, source_identity, session_id)
    if state is None or state["disposition"] != "mirrored":
        return None
    return {key: state[key] for key in (
        "destination", "message_id", "card_id", "receipt_kind")}


def _send_attempts(messages):
    calls = {}
    results = {}
    for item in messages or ():
        _check_deadline()
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
    except TimeoutError:
        raise
    except Exception:
        return None
    calls = {}
    for (raw_calls,) in assistant_rows:
        _check_deadline()
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


def _persisted_action_cards(path, session_id, request_id, source_timestamp):
    """Recover successful owner actions from the exact persisted source turn."""
    try:
        with _open_readonly(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if not {"id", "platform_message_id"} <= columns:
                return None
            source_rows = db.execute(
                "SELECT id FROM messages WHERE session_id=? AND role='user' "
                "AND platform_message_id=? AND timestamp=? ORDER BY id LIMIT 2",
                (session_id, request_id, source_timestamp),
            ).fetchall()
            if len(source_rows) != 1:
                return None
            source_row = source_rows[0]
            next_user = db.execute(
                "SELECT MIN(id) FROM messages WHERE session_id=? AND role='user' AND id>?",
                (session_id, source_row[0]),
            ).fetchone()
            sql = (
                "SELECT tool_name, content FROM messages WHERE session_id=? AND role='tool' "
                "AND id>?"
            )
            params = [session_id, source_row[0]]
            if next_user and next_user[0] is not None:
                sql += " AND id<?"
                params.append(next_user[0])
            rows = db.execute(sql + " ORDER BY id", params).fetchall()
    except TimeoutError:
        raise
    except Exception:
        return None
    cards = set()
    for tool_name, content in rows:
        _check_deadline()
        if str(tool_name or "") not in _ACTION_TOOLS:
            continue
        result = _json(content)
        if _succeeded(result):
            cards.update(_CARD.findall(json.dumps(result, sort_keys=True)))
    return {
        "cards": cards,
        "source_is_current": not next_user or next_user[0] is None,
    }


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
        _check_deadline()
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


def _invalid_after_attempt(identity):
    return _assessment(
        identity, "completion_evidence_invalid_after_transport_attempt",
        repair="Reconcile the invalid completion evidence and persisted transport attempt; do not resend.",
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
    identity = _source_identity(profile, source)
    durable_attempt = _durable_receipt_attempt(settings, identity, session_id)
    text = user_message if isinstance(user_message, str) else ""
    if not _source_is_receipt_eligible(text, source.get("timestamp")):
        return None
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
    state_path = settings.get("state_db_path")
    try:
        persisted_source = _assessment_source_row(state_path, session_id, request_id, channel)
    except ValueError as exc:
        reason = str(exc)
        if reason == "source_database_unavailable":
            return _invalid_after_attempt(identity) if durable_attempt else _reconcile(identity)
        if reason == "source_row_missing":
            return (_invalid_after_attempt(identity) if durable_attempt
                    else _assessment(identity, "source_not_persisted"))
        return _invalid_after_attempt(identity) if durable_attempt else _assessment(identity, reason)
    if persisted_source[1] != source.get("timestamp"):
        return (_invalid_after_attempt(identity) if durable_attempt
                else _assessment(identity, "source_not_persisted"))
    if not _source_is_receipt_eligible(persisted_source[3], persisted_source[1]):
        return None
    acted, verified_action_cards = _action_status(final_response, messages)
    persisted_action = _persisted_action_cards(
        state_path, session_id, request_id, source.get("timestamp"))
    if persisted_action is None:
        return (_invalid_after_attempt(identity) if durable_attempt
                else _reconcile(identity, "action_evidence_unavailable"))
    persisted_action_cards = persisted_action["cards"]
    if not persisted_action["source_is_current"]:
        return None
    if persisted_action_cards:
        acted = True
        verified_action_cards.update(persisted_action_cards)
    if not acted:
        return _invalid_after_attempt(identity) if durable_attempt else None
    if source_cards:
        action_cards = verified_action_cards & source_cards
    else:
        source_cards = set(verified_action_cards)
        action_cards = set(verified_action_cards)
    if not source_cards:
        return (_invalid_after_attempt(identity) if durable_attempt
                else _reconcile(identity, "owner_card_unresolved"))

    expected_target = str(settings.get("telegram_destination") or "")
    expected_platform, separator, expected_chat_id = expected_target.partition(":")
    if separator != ":" or expected_platform != "telegram" or not expected_chat_id:
        return (_invalid_after_attempt(identity) if durable_attempt
                else _reconcile(identity, "destination_configuration_unavailable"))
    destination_session = str(settings.get("telegram_session_id") or "")
    board_path = settings.get("kanban_db_path")
    if not action_cards:
        return (_invalid_after_attempt(identity) if durable_attempt
                else _assessment(identity, "action_unverified"))
    durable_state = _durable_receipt_state(settings, identity, session_id)
    if (durable_state and durable_state.get("card_id") in action_cards & source_cards
            and durable_state["disposition"] == "mirrored"):
        durable = {key: durable_state[key] for key in (
            "destination", "message_id", "card_id", "receipt_kind")}
        return {
            "verdict": "reproduced", "mismatch": "", "subtype": "missing_chat_telegram_receipt",
            "source_identity": identity, "receipt": durable,
        }
    if durable_state and durable_state.get("card_id") in action_cards & source_cards:
        if durable_state["disposition"] == "transport_only":
            return _assessment(
                identity, "mirror_missing",
                repair="Repair the destination-session mirror only; transport succeeded, so do not resend.",
            )
        if durable_state["disposition"] == "reconciliation":
            return _assessment(
                identity, "transport_reconciliation_no_resend",
                repair="Reconcile the persisted transport and mirror evidence; do not resend until absence is proven.",
            )
    if durable_attempt:
        return _invalid_after_attempt(identity)
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
        _check_deadline()
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
        _check_deadline()
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
