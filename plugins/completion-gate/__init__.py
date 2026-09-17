"""General completion-gate plugin; shared installation, profile-local opt-in."""
import logging
import os
import time

from .gate import Gate
from .checks import bounded_check
from .extraction import bounded_extract
from .escalation import send, message

logger = logging.getLogger(__name__)
DEFAULTS = {"enabled": False, "max_blocks": 2, "check_timeout": 10,
            "extract_timeout": 10, "total_timeout": 20, "max_claims": 20,
            "max_answer_chars": 32000}


def register(ctx):
    from .cli import register_cli
    register_cli(ctx)
    def before_turn_end(final_response, session_id="", task_id="", turn_id="",
                        already_blocked=False, can_continue=True, user_message=None, **kwargs):
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
            deadline = time.monotonic() + float(settings["total_timeout"])
            def extract(answer):
                if len(answer) > int(settings["max_answer_chars"]):
                    raise ValueError("answer_limit")
                return bounded_extract(answer, timeout=min(float(settings["extract_timeout"]), remaining()),
                                       max_claims=int(settings["max_claims"]))
            def remaining():
                return max(0.001, deadline - time.monotonic())
            def check(claim):
                if time.monotonic() >= deadline:
                    return "unverified", "gate_deadline"
                return bounded_check(claim, timeout=min(float(settings["check_timeout"]), remaining()))
            gate = Gate(settings, extract=extract, check=check, escalate=send)
            # Suppress only a gate-authored, durably recorded inbound. A magic prefix is not enough.
            if isinstance(user_message, str):
                for event in gate.events():
                    esc = event["escalation"]
                    if esc and esc["target"] == profile and message({**esc, "event_id": event["id"]}) == user_message:
                        return None
            return gate.evaluate(final_response, profile=profile, task_id=chain, turn_id=turn_id,
                                 already_blocked=already_blocked, can_continue=can_continue)
        except Exception as exc:
            logger.warning("Completion gate adapter failed open (%s)", type(exc).__name__)
            return None
    ctx.register_hook("before_turn_end", before_turn_end)
