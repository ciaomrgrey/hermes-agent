"""Deterministic read-only probes. Never execute a claimed command or follow HTTP redirects."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request


def result(ok, mismatch):
    return ("reproduced", "") if ok else ("failed", mismatch)


# Accepted, non-deficit kinds: the claim is real but no local read-only probe exists.
BY_DESIGN = {"agent_report", "external_state", "qualitative", "turn_receipt"}
MAX_HASH_BYTES = 256 * 1024 * 1024
DROPBOX_PREFIXES = ("dropbox:", "Dropbox/")


def dropbox_root():
    """The desktop client's personal root (~/.dropbox/info.json), else ~/Dropbox."""
    home = Path.home()
    try:
        info = json.loads((home / ".dropbox/info.json").read_text())
        path = Path((info.get("personal") or info.get("business") or {})["path"])
        if path.is_absolute():
            return path
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return home / "Dropbox"


def resolve_path(ref):
    """Absolute, ``~/``-prefixed or Dropbox-relative; NEVER relative to the cwd."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    for prefix in DROPBOX_PREFIXES:
        if ref.startswith(prefix):
            rel = Path(ref[len(prefix):].lstrip("/"))
            if not rel.parts or ".." in rel.parts:
                return None
            return dropbox_root() / rel
    if ref == "~" or ref.startswith("~/"):
        path = Path(ref).expanduser()
        return path if path.is_absolute() and ".." not in path.parts else None
    path = Path(ref)
    return path if path.is_absolute() else None


def file_check(ref, timeout):
    p = resolve_path(ref)
    if p is None:
        return "unverified", "absolute_path_required"
    if p.is_dir():
        return result(any(p.iterdir()), "directory missing or empty")
    return result(p.is_file() and p.stat().st_size > 0, "file missing or empty")


def file_hash_check(ref, timeout):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"} or not isinstance(ref["sha256"], str):
        return "unverified", "invalid_reference"
    expected = ref["sha256"].strip().rstrip("….").casefold()
    if not re.fullmatch(r"[0-9a-f]{8,64}", expected):
        return "unverified", "sha256_prefix_required"
    p = resolve_path(ref["path"])
    if p is None:
        return "unverified", "absolute_path_required"
    if p.is_dir():
        return "unverified", "not_a_file"
    if not p.is_file():
        return "failed", "file missing"
    if p.stat().st_size > MAX_HASH_BYTES:
        return "unverified", "file_too_large"
    digest = hashlib.sha256()
    deadline = time.monotonic() + timeout
    with p.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if time.monotonic() >= deadline:
                raise TimeoutError()
            digest.update(block)
    actual = digest.hexdigest()
    return result(actual.startswith(expected), f"sha256 mismatch: {actual[:12]}…, expected {expected[:12]}…")


CARD_ID = re.compile(r"t_[0-9a-f]{8}")
CARD_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
# A dispatcher claim (review|ready -> running, assignee unchanged) may land between the agent's own
# handoff and this check. Only that one transition is undone, and only while it is the card's
# CURRENT run and recent: no other history counts as present state.
CLAIM_RACE_SECONDS = 15 * 60


def kanban_boards():
    """Native board layout: HERMES_KANBAN_DB pin, legacy default DB, then every named board."""
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    root = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if not root:
        from hermes_constants import get_default_hermes_root
        root = str(get_default_hermes_root())
    root = Path(root).expanduser()
    candidates = ([Path(pinned).expanduser()] if pinned else []) + [root / "kanban.db"]
    boards = root / "kanban/boards"
    if boards.is_dir():
        candidates += sorted(p / "kanban.db" for p in boards.iterdir() if p.is_dir() and not p.name.startswith("_"))
    seen, found = set(), []
    for path in candidates:
        # A zero-byte kanban.db is an unused placeholder, not a board.
        if path.is_absolute() and path.is_file() and path.stat().st_size > 0 and path.resolve() not in seen:
            seen.add(path.resolve())
            found.append(path)
    return found


def card_row(db, card_id, now):
    """(status, assignee, current_run_id) of a card. Seam for as-of replay."""
    return db.execute("SELECT status, assignee, current_run_id FROM tasks WHERE id=?", (card_id,)).fetchone()


def pre_claim_status(db, card_id, run_id, now):
    """Status the card had just before the dispatcher's claim that opened the CURRENT run, if recent."""
    if run_id is None:
        return None
    event = db.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='claimed' AND created_at BETWEEN ? AND ?"
        " ORDER BY id DESC LIMIT 1", (card_id, now - CLAIM_RACE_SECONDS, now)).fetchone()
    try:
        payload = json.loads(event[0] or "{}") if event else None
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        return None
    # Native kanban_db: source_status=review marks a reviewer claim; every other claim starts at ready.
    return "review" if payload.get("source_status") == "review" else "ready"


def card_check(ref, timeout):
    if isinstance(ref, str):
        ref = {"id": ref}
    if not isinstance(ref, dict) or "id" not in ref or not set(ref) <= {"id", "status", "assignee"}:
        return "unverified", "invalid_reference"
    if not isinstance(ref["id"], str) or not CARD_ID.fullmatch(ref["id"]):
        return "unverified", "card_id_required"
    want = {k: ref[k] for k in ("status", "assignee") if k in ref}
    if not all(isinstance(v, str) for v in want.values()):
        return "unverified", "invalid_reference"
    want = {k: v.strip().casefold() for k, v in want.items()}
    if want.get("status", "ready") not in CARD_STATUSES:
        return "unverified", "invalid_reference"
    boards = kanban_boards()
    if not boards:
        return "unverified", "kanban_board_unavailable"
    unreadable = False
    now = time.time()
    for path in boards:
        try:
            db = readonly_db(path, timeout)
        except Exception:
            unreadable = True
            continue
        try:
            row = card_row(db, ref["id"], now)
            if row is None:
                continue
            status, assignee = (row[0] or "").casefold(), (row[1] or "").casefold()
            if "assignee" in want and assignee != want["assignee"]:
                known = db.execute("SELECT 1 FROM tasks WHERE lower(assignee)=? UNION ALL"
                                   " SELECT 1 FROM task_runs WHERE lower(profile)=? LIMIT 1",
                                   (want["assignee"], want["assignee"])).fetchone()
                if known is None:
                    # Prose alias ("Hermes"), not a profile name this board assigns.
                    return "unverified", "assignee_not_a_board_profile"
                return "failed", f"card assignee {assignee}, expected {want['assignee']}"
            if "status" in want and status != want["status"]:
                # Joint state: the assignee already matched the present row, which a claim never changes.
                if status != "running" or pre_claim_status(db, ref["id"], row[2], now) != want["status"]:
                    return "failed", f"card status {status}, expected {want['status']}"
            return "reproduced", ""
        except sqlite3.DatabaseError:
            unreadable = True
        finally:
            db.close()
    return ("unverified", "kanban_board_unavailable") if unreadable else ("failed", "card absent")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_check(ref, timeout):
    parsed = urlsplit(ref["url"])
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "unverified", "unsafe_or_ambiguous_url"
    request = urllib.request.Request(ref["url"], method="HEAD")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status in {401, 403, 429} and status != int(ref["status"]):
        # Auth walls and rate limits describe this client, not the claimed resource.
        return "unverified", "unverifiable_from_client"
    return result(status == int(ref["status"]), f"HTTP status {status}, expected {int(ref['status'])}")


def socket_check(ref, timeout):
    with socket.create_connection((ref["host"], int(ref["port"])), timeout=timeout):
        return "reproduced", ""


def process_check(ref, timeout):
    pid = int(ref["pid"])
    if pid <= 0:
        return "unverified", "positive_pid_required"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "failed", "process absent"
    return "reproduced", ""


class YamlParserUnavailable(Exception):
    """No safe YAML loader is importable, so a YAML artefact cannot be verified either way."""


def yaml_safe_load():
    # v0.21.6+ ships hermes_yaml (ruamel, safe) and drops PyYAML; older engines have only PyYAML.
    try:
        from hermes_yaml import safe_load
        return safe_load
    except ImportError:
        pass
    try:
        from yaml import safe_load
        return safe_load
    except ImportError:
        raise YamlParserUnavailable() from None


def read_document(path):
    path = Path(path)
    if not path.is_absolute() or path.suffix not in {".json", ".yaml", ".yml"}:
        raise ValueError("document_path")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("document_size")
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    return yaml_safe_load()(text)


def config_check(ref, timeout):
    value = read_document(ref["path"])
    for key in ref["key"].split("."):
        if re.search(r"secret|password|token|api_key|credential", key, re.I):
            return "unverified", "sensitive_config_key"
        if not isinstance(value, dict) or key not in value:
            return "failed", "config key absent"
        value = value[key]
    return result(type(value) is type(ref["expected"]) and value == ref["expected"], "config value mismatch")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("invalid_identifier")
    return '"' + value + '"'


def readonly_db(path, timeout):
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("absolute_path_required")
    db = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=timeout)
    db.execute("PRAGMA query_only=ON")
    return db


def sqlite_check(ref, timeout):
    where = ref["where"]
    if not isinstance(where, dict) or not where:
        return "unverified", "exact_row_required"
    sql = f"SELECT 1 FROM {identifier(ref['table'])} WHERE " + " AND ".join(f"{identifier(k)} IS ?" for k in where) + " LIMIT 1"
    db = readonly_db(ref["db_path"], timeout)
    try:
        return result(db.execute(sql, list(where.values())).fetchone() is not None, "SQLite row absent")
    finally:
        db.close()


def command_exit_check(ref, timeout):
    # Only a native terminal tool result is evidence, never a report or an executable string.
    if type(ref.get("expected")) is not int:
        return "unverified", "integer_exit_required"
    db = readonly_db(ref["db_path"], timeout)
    try:
        row = db.execute("SELECT content FROM messages WHERE id=? AND session_id=? AND role='tool' AND tool_name='terminal'",
                         (ref["message_id"], ref["session_id"])).fetchone()
        if row is None:
            return "unverified", "native_terminal_receipt_absent"
        receipt = json.loads(row[0])
        code = receipt.get("exit_code")
        if type(code) is not int:
            return "unverified", "terminal_exit_unavailable"
        return result(code == ref["expected"], f"command exit {code}, expected {ref['expected']}")
    finally:
        db.close()


def cron_check(ref, timeout):
    data = read_document(ref["path"])
    jobs = data["jobs"] if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return "unverified", "unknown_cron_schema"
    return result(any(isinstance(j, dict) and j.get("id") == ref["id"] for j in jobs), "cron record absent")


CHECKERS = {"file": file_check, "http": http_check, "socket": socket_check,
            "process": process_check, "config": config_check, "sqlite": sqlite_check,
            "kanban": sqlite_check, "command_exit": command_exit_check, "cron": cron_check,
            "card": card_check, "file_hash": file_hash_check}


def check(kind, ref, timeout=10):
    if kind in BY_DESIGN:
        return "unverified", "unverifiable_by_design"
    checker = CHECKERS.get(kind)
    if checker is None:
        return "unverified", "unknown_kind"
    try:
        return checker(ref, timeout)
    except (ConnectionRefusedError, ProcessLookupError):
        return "failed", "target not responding"
    except YamlParserUnavailable:
        return "unverified", "yaml_parser_unavailable"
    except Exception:
        # Invalid schemas, absent permissions, timeout and malformed evidence aren't false claims.
        return "unverified", "check_unavailable"


def bounded_check(claim, timeout=10, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    try:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError()
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                              # The child resolves parsers (hermes_yaml) exactly as this process does, not via inherited env.
                              input=json.dumps({"claim": claim, "deadline": deadline, "sys_path": sys.path}), text=True,
                              capture_output=True, timeout=timeout, check=True)
        verdict, mismatch = json.loads(proc.stdout)
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if verdict not in {"reproduced", "failed", "unverified"} or not isinstance(mismatch, str):
            raise ValueError("invalid_checker_result")
        return verdict, mismatch
    except (subprocess.TimeoutExpired, TimeoutError):
        raise TimeoutError() from None
    except Exception:
        return "unverified", "checker_timeout_or_error"


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    sys.path[:] = [p for p in payload.get("sys_path", ()) if isinstance(p, str)] or sys.path
    claim = payload["claim"]
    remaining = payload['deadline'] - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    print(json.dumps(check(claim["artefact_kind"], claim["artefact_ref"], remaining)))
