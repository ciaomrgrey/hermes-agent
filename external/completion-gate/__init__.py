"""General completion-gate plugin; shared installation, profile-local opt-in."""
import logging
import os
import time

from .gate import Gate
from .checks import bounded_check
from .extraction import bounded_extract
from .escalation import send, message
from .diagnostics import failure
from . import receipts

logger = logging.getLogger(__name__)
DEFAULTS = {"enabled": False, "max_blocks": 2, "check_timeout_seconds": 60, "max_claims": 20,
            "max_answer_chars": 32000, "chat_receipts_enabled": False,
            "inaction_enabled": False, "chat_source_channel": "C0BTEFMAAJX",
            "telegram_destination": "", "telegram_session_id": "",
            "state_db_path": "", "kanban_db_path": ""}


def with_outcome(verdict, evaluated):
    """Attach the evaluated verdict as display-only ``gate_outcome``.

    Hosts honour only ``{"action": "block", "message": ...}``; a dict without ``action`` is
    ignored by every core, so attaching the outcome to a delivery cannot change control flow.
    """
    if evaluated is None:
        return verdict
    if isinstance(verdict, dict):
        return {**verdict, "gate_outcome": evaluated}
    return {"gate_outcome": evaluated}


def register(ctx):
    from .cli import register_cli
    register_cli(ctx)
    def before_turn_end(final_response, session_id="", task_id="", turn_id="",
                        already_blocked=False, can_continue=True, user_message=None,
                        messages=None, source_identity=None, **kwargs):
        # One monotonic clock from hook entry covers assessment, extraction,
        # checks, audit and escalation. check_timeout_seconds is its only knob.
        started = time.monotonic()
        from hermes_constants import get_hermes_home, get_default_hermes_root, profile_name_for_home
        try:
            # Never cache the switch: CLI/gateway live config readback must take effect next call.
            settings = {k: ctx.get_config(k, v) for k, v in DEFAULTS.items()}
            if settings["enabled"] is not True:
                return None
            settings["db_path"] = ctx.get_config("db_path", str(get_default_hermes_root() / "state/completion-gate/gate.db"))
            profile = profile_name_for_home(get_hermes_home()) or "default"
            chain = os.environ.get("HERMES_KANBAN_TASK") or session_id or task_id
            if not chain or not turn_id:
                raise ValueError("missing_stable_identity")
            deadline = started + float(settings["check_timeout_seconds"])
            assessment_by_kind = {}
            def assess():
                """Receipt/inaction assessments, run inside Gate.evaluate on the same clock."""
                assessments = []
                if settings["chat_receipts_enabled"] is True:
                    remaining()
                    assessment = receipts.assess_chat_receipt(
                        profile=profile, source=source_identity or {}, user_message=user_message,
                        final_response=final_response, messages=messages or (), session_id=session_id,
                        settings=settings,
                    )
                    remaining()
                    if assessment:
                        identity = assessment["source_identity"]
                        prior = Gate(settings, extract=lambda _: []).receipt_binding(identity)
                        if receipts.binding_current(
                                settings, prior, source_identity=identity, session_id=session_id):
                            assessment = {**assessment, "verdict": "reproduced", "mismatch": "", "receipt": prior}
                        remaining()
                        assessments.append(({
                            "claim": assessment.get("repair", "Chat decision Telegram receipt"),
                            "artefact_kind": "chat_telegram_receipt", "artefact_ref": identity,
                        }, assessment))
                if settings["inaction_enabled"] is True:
                    remaining()
                    assessment = receipts.assess_inaction(
                        final_response, messages or (), user_message=user_message or "")
                    remaining()
                    if assessment:
                        assessments.append(({
                            "claim": assessment["repair"], "artefact_kind": "inaction_followthrough",
                            "artefact_ref": {"turn_id": turn_id},
                        }, assessment))
                assessment_by_kind.clear()
                assessment_by_kind.update({claim["artefact_kind"]: a for claim, a in assessments})
                return [claim for claim, _ in assessments]
            def extract(answer):
                if len(answer) > int(settings["max_answer_chars"]):
                    raise ValueError("answer_limit")
                internal = assess()
                remaining()
                claims = bounded_extract(answer, max_claims=int(settings["max_claims"]), deadline=deadline)
                remaining()
                return claims + internal
            def remaining():
                value = deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError()
                return value
            def check(claim):
                if claim["artefact_kind"] in assessment_by_kind:
                    assessment = assessment_by_kind[claim["artefact_kind"]]
                    evidence = {k: assessment[k] for k in ("subtype", "source_identity", "receipt") if k in assessment}
                    return assessment["verdict"], assessment["mismatch"], evidence
                remaining()
                result = bounded_check(claim, deadline=deadline)
                remaining()
                return result
            gate = Gate(settings, extract=extract, check=check,
                        escalate=lambda event: send(event, deadline=deadline))
            # Suppress only a gate-authored, durably recorded inbound. A magic prefix is not enough.
            verdict = gate.evaluate(final_response, profile=profile, task_id=chain, turn_id=turn_id,
                                    already_blocked=already_blocked, can_continue=can_continue,
                                    deadline=deadline, user_message=user_message)
            return with_outcome(verdict, gate.last_outcome)
        except Exception as exc:
            logger.warning("Completion gate adapter failed open %s", failure(exc))
            return None
    ctx.register_hook("before_turn_end", before_turn_end)
