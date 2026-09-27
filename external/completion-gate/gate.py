"""Append-only evidence and atomic, persistent block reservations."""
from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter
from .diagnostics import failure, sanitize

EVALUATED_ACTIONS = {"deliver", "block", "fail_open", "reentrant_pass", "gate_error"}

logger = logging.getLogger(__name__)
_evaluating = ContextVar("completion_gate_evaluating", default=False)
_deadline = ContextVar("completion_gate_deadline", default=None)


class DeadlineConnection(sqlite3.Connection):
    """Refresh SQLite's busy wait from the same clock before each statement."""
    def _remaining(self):
        deadline = _deadline.get()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            super().execute(f'PRAGMA busy_timeout={math.ceil(remaining * 1000)}')

    def execute(self, *args, **kwargs):
        self._remaining()
        return super().execute(*args, **kwargs)

    def commit(self):
        self._remaining()
        return super().commit()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def normalized(value):
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


class Gate:
    def __init__(self, settings, *, extract, escalate=None, check=None):
        self.settings, self.extract = settings, extract
        self.escalate = escalate
        from .checks import bounded_check
        self.check = check or bounded_check
        self.path = Path(settings["db_path"])

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = _deadline.get()
        remaining = 1 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        db = sqlite3.connect(self.path, timeout=remaining, factory=DeadlineConnection)
        if deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            self.path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created REAL, profile TEXT, task_id TEXT,
                turn_id TEXT, action TEXT, claims TEXT, block_count INTEGER,
                reason_hashes TEXT, escalation TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS events_chain ON events(profile,task_id)")
            # Serialize additive migration so concurrent first-use workers cannot race.
            db.execute("BEGIN IMMEDIATE")
            if 'diagnostics' not in {r[1] for r in db.execute('PRAGMA table_info(events)')}:
                db.execute("ALTER TABLE events ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'")
            db.commit()
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_advice(self, *, profile, task_id, marker):
        if marker not in {"advised", "tried"}:
            raise ValueError("advice marker must be advised or tried")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            history = self._history(db, profile, task_id)
            if marker == "tried" and not any(r["action"] == "advice_advised" for r in history):
                raise ValueError("record Gurney advice before marking it tried")
            if not any(r["action"] == "advice_" + marker for r in history):
                self._append(db, profile, task_id, "", "advice_" + marker, [],
                             sum(r["action"] == "block" for r in history))

    def events(self):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")]
        for row in rows:
            if _deadline.get() is not None and time.monotonic() >= _deadline.get():
                raise TimeoutError()
            for key in ("claims", "reason_hashes", "escalation", "diagnostics"):
                row[key] = json.loads(row[key])
        return rows

    def metrics(self, since=None, until=None):
        since = float(since) if since is not None else None
        until = float(until) if until is not None else None
        rows = [r for r in self.events()
                if (since is None or r["created"] >= since)
                and (until is None or r["created"] < until)]

        def aggregate(scoped):
            claims = [c for r in scoped for c in r["claims"]]
            unverified = sum(c["verdict"] == "unverified" for c in claims)
            actions = Counter(r["action"] for r in scoped)
            evaluated = sum(actions[action] for action in EVALUATED_ACTIONS)
            subtypes = Counter(c["subtype"] for c in claims if c.get("subtype"))
            causes = Counter()
            for row in scoped:
                if row["action"] == "gate_error":
                    data = row.get("diagnostics")
                    cause = (data or {}).get("cause") if isinstance(data, dict) else None
                    causes[cause or "unknown"] += 1
            # Error sentinels remain in claim coverage. Reliability instead uses
            # evaluated outcomes, never advice/escalation bookkeeping rows.
            return {"claims": len(claims), "unverified": unverified,
                    "unverified_rate": unverified / len(claims) if claims else 0.0,
                    "blocks": actions["block"],
                    "escalations": sum(bool(r["escalation"]) for r in scoped),
                    "evaluated_turns": evaluated, "action_counts": dict(actions),
                    "gate_errors": actions["gate_error"],
                    "gate_error_rate": actions["gate_error"] / evaluated if evaluated else 0.0,
                    "gate_error_causes": dict(causes), "subtypes": dict(subtypes)}

        return {**aggregate(rows), "window": {"since": since, "until": until}, "per_profile": {
            profile: aggregate([r for r in rows if r["profile"] == profile])
            for profile in sorted({r["profile"] for r in rows})}}

    def receipt_binding(self, source_identity):
        """Return a prior reproduced exact-source receipt, if one survived replay/restart."""
        for row in reversed(self.events()):
            for claim in row["claims"]:
                if (claim.get("verdict") == "reproduced"
                        and claim.get("subtype") == "missing_chat_telegram_receipt"
                        and claim.get("source_identity") == source_identity
                        and isinstance(claim.get("receipt"), dict)):
                    return claim["receipt"]
        return None

    def _append(self, db, profile, task, turn, action, claims, count, hashes=(), escalation=None, diagnostics=None):
        cursor = db.execute("""INSERT INTO events
            (created,profile,task_id,turn_id,action,claims,block_count,reason_hashes,escalation,diagnostics)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (time.time(), profile, task, turn, action,
            json.dumps(claims), count, json.dumps(list(hashes)), json.dumps(escalation),
            json.dumps(sanitize(diagnostics) if diagnostics else {})))
        return cursor.lastrowid

    def _history(self, db, profile, task):
        return list(db.execute("SELECT * FROM events WHERE profile=? AND task_id=?", (profile, task)))

    def evaluate(self, answer, *, profile, task_id, turn_id, already_blocked=False, can_continue=True,
                 deadline=None, user_message=None):
        if self.settings.get("enabled") is not True:
            return None
        if _evaluating.get():
            logger.info("Completion gate recursive invocation passed through")
            return None
        token = _evaluating.set(True)
        clock_token = _deadline.set(deadline)
        started = time.monotonic()
        try:
            if deadline is not None and started >= deadline:
                raise TimeoutError()
            if isinstance(user_message, str):
                from .escalation import message
                for event in self.events():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError()
                    esc = event['escalation']
                    if esc and esc['target'] == profile and message({**esc, 'event_id': event['id']}) == user_message:
                        return None
            return self._evaluate(answer, profile, task_id, turn_id, already_blocked, can_continue, deadline)
        except Exception as exc:
            exhausted = deadline is not None and time.monotonic() >= deadline
            # Only diagnostic persistence may outlive the work clock (1s DB wait).
            _deadline.set(None)
            # Never record exceptions' messages, the answer or arbitrary reference values.
            diagnostics = failure(exc, elapsed=time.monotonic() - started)
            if exhausted:
                # The trusted parent clock supersedes the operation error. Keep
                # route/attempt/exit metadata, but normalize the class too so
                # every subsequent sanitizer derives the same timeout cause.
                diagnostics = sanitize({**diagnostics, 'exception_class': 'TimeoutError'})
            logger.warning("Completion gate failed open %s", json.dumps(diagnostics, sort_keys=True))
            try:
                with self.connect() as db:
                    count = sum(r["action"] == "block" for r in self._history(db, profile, task_id))
                    self._append(db, profile, task_id, turn_id, "gate_error",
                                 [{"verdict": "unverified", "mismatch": "gate_error"}], count,
                                 diagnostics=diagnostics)
            except Exception:
                logger.warning("Completion gate audit unavailable; delivery still allowed")
            return None
        finally:
            _deadline.reset(clock_token)
            _evaluating.reset(token)

    def _evaluate(self, answer, profile, task, turn, already_blocked, can_continue, deadline=None):
        def ensure_time():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError()
        ensure_time()
        with self.connect() as db:
            history = self._history(db, profile, task)
            ensure_time()
            if already_blocked or any(r["turn_id"] == turn and r["action"] == "block" for r in history):
                self._append(db, profile, task, turn, "reentrant_pass", [],
                             sum(r["action"] == "block" for r in history))
                return None
        # No database lock is held during extraction or I/O.
        results, failures = [], []
        for claim in self.extract(answer):
            ensure_time()
            checked = self.check(claim)
            ensure_time()
            verdict, mismatch = checked[:2]
            evidence = checked[2] if len(checked) > 2 and isinstance(checked[2], dict) else {}
            reason = digest([normalized(claim["claim"]), claim["artefact_kind"], claim["artefact_ref"], normalized(mismatch)])
            from .checks import CHECKERS
            internal_kinds = {"chat_telegram_receipt", "inaction_followthrough"}
            kind = claim["artefact_kind"] if claim["artefact_kind"] in CHECKERS or claim["artefact_kind"] in internal_kinds else "unknown"
            results.append({"claim_hash": digest(claim), "artefact_kind": kind,
                            "verdict": verdict, "mismatch": mismatch, "reason_hash": reason,
                            **evidence})
            if verdict == "failed":
                failures.append((reason, claim, mismatch))
        ensure_time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            history = self._history(db, profile, task)
            ensure_time()
            blocks = [r for r in history if r["action"] == "block"]
            count = len(blocks)
            prior = {h for r in blocks for h in json.loads(r["reason_hashes"])}
            hashes = [f[0] for f in failures]
            action, escalation = "deliver", None
            if any(r["turn_id"] == turn for r in blocks):
                action = "reentrant_pass"
            elif failures:
                repeat = bool(prior.intersection(hashes))
                ceiling = count >= max(0, int(self.settings.get("max_blocks", 2)))
                advice_failed = any(r["action"] == "advice_tried" for r in history)
                if repeat or ceiling or advice_failed or not can_continue:
                    action = "fail_open"
                    escalation = {"reason": "repeat_reason" if repeat else "retry_ceiling",
                                  "target": "gurney" if profile == "generalist" else "generalist",
                                  "profile": profile, "task_id": task, "block_count": count}
                    if not can_continue:
                        escalation["reason"] = "turn_budget_exhausted"
                    if advice_failed:
                        escalation.update(reason="advice_failed_again", target="gurney")
                    if any(json.loads(r["escalation"]) and json.loads(r["escalation"]).get("reason") == escalation["reason"] for r in history):
                        escalation = None
                else:
                    action, count = "block", count + 1
            # Reserve the existing escalation/dedup key before I/O, without
            # prematurely recording an evaluated fail_open. Expiry must have one
            # outcome, gate_error, not fail_open followed by gate_error. This
            # append-only bookkeeping row also supplies the inbound event_id.
            pending = bool(escalation and self.escalate)
            event_id = self._append(db, profile, task, turn,
                                    "escalation_pending" if pending else action,
                                    [] if pending else results, count, hashes, escalation)
        if pending:
            try:
                ensure_time()
                receipt = self.escalate({**escalation, "event_id": event_id})
                ensure_time()
                status = receipt.get("status", "unverified") if isinstance(receipt, dict) else "unverified"
            except TimeoutError:
                raise
            except Exception:
                ensure_time()
                status = "failed"
            with self.connect() as db:
                self._append(db, profile, task, turn, action, results, count, hashes)
                self._append(db, profile, task, turn, "escalation_" + status, [], count)
        if action == "block":
            return {"action": "block", "message": "Completion evidence mismatch:\n" + "\n".join(
                f"{claim['claim']} [{claim['artefact_ref']}]: {mismatch}" for _, claim, mismatch in failures
            ) + "\nReproduce/fix these artefacts or correct the claim. Do not retry unsafe operations."}
        return None
