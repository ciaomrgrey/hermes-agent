"""Deterministic read-only probes. Never execute a claimed command or follow HTTP redirects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
import urllib.error
import urllib.request


def result(ok, mismatch):
    return ("reproduced", "") if ok else ("failed", mismatch)


def file_check(ref, timeout):
    p = Path(ref)
    if not p.is_absolute():
        return "unverified", "absolute_path_required"
    return result(p.is_file() and p.stat().st_size > 0, "file missing or empty")


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


def read_document(path):
    path = Path(path)
    if not path.is_absolute() or path.suffix not in {".json", ".yaml", ".yml"}:
        raise ValueError("document_path")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("document_size")
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml
    return yaml.safe_load(text)


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
            "kanban": sqlite_check, "command_exit": command_exit_check, "cron": cron_check}


def check(kind, ref, timeout=10):
    checker = CHECKERS.get(kind)
    if checker is None:
        return "unverified", "unknown_kind"
    try:
        return checker(ref, timeout)
    except (ConnectionRefusedError, ProcessLookupError):
        return "failed", "target not responding"
    except Exception:
        # Invalid schemas, absent permissions, timeout and malformed evidence aren't false claims.
        return "unverified", "check_unavailable"


def bounded_check(claim, timeout=10):
    try:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                              input=json.dumps({"claim": claim, "timeout": timeout}), text=True,
                              capture_output=True, timeout=timeout, check=True)
        verdict, mismatch = json.loads(proc.stdout)
        if verdict not in {"reproduced", "failed", "unverified"} or not isinstance(mismatch, str):
            raise ValueError("invalid_checker_result")
        return verdict, mismatch
    except Exception:
        return "unverified", "checker_timeout_or_error"


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    claim = payload["claim"]
    print(json.dumps(check(claim["artefact_kind"], claim["artefact_ref"], payload["timeout"])))
