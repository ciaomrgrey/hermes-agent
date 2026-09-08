"""Turn-end guard for kanban workers, which must end with a lifecycle transition.

Some models narrate the next step and stop with no tool calls; Hermes treats that
as a clean exit → ``rc=0`` → dispatcher ``protocol_violation``. Policy-only:
return a bounded synthetic nudge so the loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set, unless ``HERMES_KANBAN_STOP_NUDGE`` disables it."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip())


def _tool_call_name(tool_call: Any) -> str:
    """Return a tool call's function name from dict or object message shapes."""
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        return str((function.get("name") if isinstance(function, dict) else tool_call.get("name")) or "")
    function = getattr(tool_call, "function", None)
    return str((getattr(function, "name", "") if function is not None else getattr(tool_call, "name", "")) or "")


def _successful_tool_result(msg: dict, *, paired_name: str = "") -> bool:
    """Whether *msg* records a successful terminal lifecycle transition."""
    if msg.get("role") != "tool":
        return False
    name = str(msg.get("name") or paired_name)
    if name not in _TERMINAL_KANBAN_TOOLS:
        return False
    payload: Any = msg.get("content")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return False
    return isinstance(payload, dict) and payload.get("ok") is True


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation recorded a successful terminal kanban tool result."""
    terminal_calls: dict[str, str] = {}
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tool_call in msg.get("tool_calls") or ():
                name = _tool_call_name(tool_call)
                if name not in _TERMINAL_KANBAN_TOOLS:
                    continue
                call_id = tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", None)
                if call_id is not None:
                    terminal_calls[str(call_id)] = name
            continue
        call_id = msg.get("tool_call_id")
        paired_name = terminal_calls.get(str(call_id), "") if call_id is not None else ""
        if _successful_tool_result(msg, paired_name=paired_name):
            return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, budget exhausted)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board transition "
        "causes a protocol violation.\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call the lifecycle tool required by the task: `kanban_complete`, "
        "`kanban_block`, `kanban_request_review`, or `kanban_request_changes`.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled", "session_called_kanban_terminal"]
