"""Generic pre-delivery text-turn hook; no plugin policy in the loop."""
import logging

from agent.message_metadata import append_message

logger = logging.getLogger(__name__)


def defers_text_delivery():
    try:
        from hermes_cli.lifecycle import has_hook
        return has_hook("before_turn_end")
    except Exception:
        logger.warning("before_turn_end lookup failed open")
        return False


def prepared_response(agent, text):
    record = getattr(agent, "_turn_end_prepared", None)
    if record and record[0] == getattr(agent, "_current_turn_id", "") and record[1][0] == text:
        return record[1]
    return None


def prepare_response(agent, text):
    """Opted-in gates must see output transforms, not a draft later replaced by a plugin."""
    if not defers_text_delivery():
        return text
    from agent.turn_finalizer import _append_file_mutation_footer, _transform_output
    text = _append_file_mutation_footer(agent, text, logger)
    value = _transform_output(agent, text, logger, platform=getattr(agent, "platform", "") or "",
                              turn_id=getattr(agent, "_current_turn_id", "") or "")
    agent._turn_end_prepared = (getattr(agent, "_current_turn_id", ""), value)
    return value[0]


def before_turn_end(agent, final_response, final_msg, messages, *, user_message, can_continue):
    if getattr(agent, "_interrupt_requested", False):
        return False
    try:
        from hermes_cli.lifecycle import invoke_hook
        if not defers_text_delivery():
            return False
        turn_id = getattr(agent, "_current_turn_id", "")
        agent._turn_end_checked = (turn_id, final_response)
        already_blocked = getattr(agent, "_turn_end_blocked_id", None) == turn_id
        results = invoke_hook("before_turn_end", final_response=final_response,
            session_id=getattr(agent, "session_id", "") or "",
            task_id=getattr(agent, "_current_task_id", "") or "", turn_id=turn_id,
            platform=getattr(agent, "platform", "cli"),
            already_blocked=already_blocked, can_continue=can_continue,
            user_message=user_message)
        if already_blocked or not can_continue:
            return False
        for result in results:
            if not isinstance(result, dict) or result.get("action") != "block":
                continue
            message = result.get("message")
            if not isinstance(message, str) or not message.strip():
                continue
            agent._turn_end_blocked_id = turn_id
            # Same alternation-safe stop-gate pair as pre_verify, but never preview a rejected answer.
            final_msg["_turn_end_synthetic"] = True
            append_message(messages, final_msg)
            append_message(messages, {"role": "user", "content": message, "_turn_end_synthetic": True})
            agent._session_messages = messages
            return True
    except Exception:
        logger.warning("before_turn_end failed open", exc_info=True)
    return False
