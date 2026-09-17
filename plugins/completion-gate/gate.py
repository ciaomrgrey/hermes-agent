"""Append-only evidence and atomic, persistent block reservations."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)
_evaluating = ContextVar("completion_gate_evaluating", default=False)


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
        self.check = check or (lambda claim: bounded_check(claim, timeout=float(settings.get("check_timeout", 10))))
        self.path = Path(settings["db_path"])

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=1)
        self.path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, created REAL, profile TEXT, task_id TEXT,
            turn_id TEXT, action TEXT, claims TEXT, block_count INTEGER,
            reason_hashes TEXT, escalation TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS events_chain ON events(profile,task_id)")
        try:
            with db:
                yield db
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
            for key in ("claims", "reason_hashes", "escalation"):
                row[key] = json.loads(row[key])
        return rows

    def metrics(self):
        rows = self.events()
        claims = [c for r in rows for c in r["claims"]]
        unverified = sum(c["verdict"] == "unverified" for c in claims)
        per_profile = {}
        for profile in sorted({r["profile"] for r in rows}):
            scoped = [r for r in rows if r["profile"] == profile]
            pc = [c for r in scoped for c in r["claims"]]
            uv = sum(c["verdict"] == "unverified" for c in pc)
            per_profile[profile] = {"claims": len(pc), "unverified": uv,
                "unverified_rate": uv / len(pc) if pc else 0.0,
                "blocks": sum(r["action"] == "block" for r in scoped),
                "escalations": sum(bool(r["escalation"]) for r in scoped)}
        return {"claims": len(claims), "unverified": unverified, "per_profile": per_profile,
                "unverified_rate": unverified / len(claims) if claims else 0.0,
                "blocks": sum(r["action"] == "block" for r in rows),
                "escalations": sum(bool(r["escalation"]) for r in rows)}

    def _append(self, db, profile, task, turn, action, claims, count, hashes=(), escalation=None):
        cursor = db.execute("""INSERT INTO events
            (created,profile,task_id,turn_id,action,claims,block_count,reason_hashes,escalation)
            VALUES(?,?,?,?,?,?,?,?,?)""", (time.time(), profile, task, turn, action,
            json.dumps(claims), count, json.dumps(list(hashes)), json.dumps(escalation)))
        return cursor.lastrowid

    def _history(self, db, profile, task):
        return list(db.execute("SELECT * FROM events WHERE profile=? AND task_id=?", (profile, task)))

    def evaluate(self, answer, *, profile, task_id, turn_id, already_blocked=False, can_continue=True):
        if self.settings.get("enabled") is not True:
            return None
        if _evaluating.get():
            logger.info("Completion gate recursive invocation passed through")
            return None
        token = _evaluating.set(True)
        try:
            return self._evaluate(answer, profile, task_id, turn_id, already_blocked, can_continue)
        except Exception as exc:
            # Never record exceptions' messages, the answer or arbitrary reference values.
            logger.warning("Completion gate failed open (%s)", type(exc).__name__)
            try:
                with self.connect() as db:
                    count = sum(r["action"] == "block" for r in self._history(db, profile, task_id))
                    self._append(db, profile, task_id, turn_id, "gate_error",
                                 [{"verdict": "unverified", "mismatch": "gate_error"}], count)
            except Exception:
                logger.warning("Completion gate audit unavailable; delivery still allowed")
            return None
        finally:
            _evaluating.reset(token)

    def _evaluate(self, answer, profile, task, turn, already_blocked, can_continue):
        with self.connect() as db:
            history = self._history(db, profile, task)
            if already_blocked or any(r["turn_id"] == turn and r["action"] == "block" for r in history):
                self._append(db, profile, task, turn, "reentrant_pass", [],
                             sum(r["action"] == "block" for r in history))
                return None
        # No database lock is held during extraction or I/O.
        results, failures = [], []
        for claim in self.extract(answer):
            verdict, mismatch = self.check(claim)
            reason = digest([normalized(claim["claim"]), claim["artefact_kind"], claim["artefact_ref"], normalized(mismatch)])
            from .checks import CHECKERS
            kind = claim["artefact_kind"] if claim["artefact_kind"] in CHECKERS else "unknown"
            results.append({"claim_hash": digest(claim), "artefact_kind": kind,
                            "verdict": verdict, "mismatch": mismatch, "reason_hash": reason})
            if verdict == "failed":
                failures.append((reason, claim, mismatch))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            history = self._history(db, profile, task)
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
            event_id = self._append(db, profile, task, turn, action, results, count, hashes, escalation)
        if escalation and self.escalate:
            try:
                receipt = self.escalate({**escalation, "event_id": event_id})
                status = receipt.get("status", "unverified") if isinstance(receipt, dict) else "unverified"
            except Exception:
                status = "failed"
            with self.connect() as db:
                self._append(db, profile, task, turn, "escalation_" + status, [], count)
        if action == "block":
            return {"action": "block", "message": "Completion evidence mismatch:\n" + "\n".join(
                f"{claim['claim']} [{claim['artefact_ref']}]: {mismatch}" for _, claim, mismatch in failures
            ) + "\nReproduce/fix these artefacts or correct the claim. Do not retry unsafe operations."}
        return None
