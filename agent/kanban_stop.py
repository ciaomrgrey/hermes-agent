"""Turn-end guard for kanban workers, which must end with ``kanban_complete`` or
``kanban_block``. Some models narrate the next step and stop with no tool calls;
Hermes treats that as a clean exit → ``rc=0`` → dispatcher ``protocol_violation``.
Policy-only: return a bounded synthetic nudge so the loop continues instead of exiting.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from typing import Any, Iterable, Optional

from agent.delegation_context import owned_kanban_task


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set for the dispatcher-owned worker, unless
    ``HERMES_KANBAN_STOP_NUDGE`` disables it. In-process delegate_task children and cron runs
    inherit the env var but own no board task and carry no kanban toolset."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
            return True
    return False


def _originating_run_active(task_id: str) -> Optional[bool]:
    """Read only: a closed/replaced run cannot be told to mutate its successor."""
    from hermes_cli.kanban_db import kanban_db_path
    raw = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    if not raw.isdecimal():
        return None
    try:
        with closing(sqlite3.connect(kanban_db_path().resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0.2)) as db:
            row = db.execute(
                "SELECT t.current_run_id,r.ended_at,r.outcome FROM tasks t "
                "JOIN task_runs r ON r.task_id=t.id WHERE t.id=? AND r.id=?",
                (task_id, int(raw))).fetchone()
        if row is None:
            return None
        current, ended, outcome = row
        return current == int(raw) and ended is None and outcome is None
    except (OSError, sqlite3.Error, ValueError):
        return None


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
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    active = _originating_run_active(tid)
    if active is False:
        return None
    if active is None:
        return (f"[System: Cannot verify originating Kanban run ownership for `{tid}`. "
                "Call `kanban_show` before any terminal mutation. A successful "
                "`kanban_complete`, `kanban_block`, `kanban_request_review` or "
                "`kanban_request_changes` closes that run. Do not mutate a successor's "
                "task or treat an attempted/failed call as success. The dispatcher "
                "retains protocol-violation accounting.]")
    return (
        "[System: You are a Hermes kanban worker. Native readback shows your "
        f"originating run for task `{tid}` is still active.\n"
        "Finish the remaining deliverable, then follow the task's review model: "
        "`kanban_complete` or `kanban_request_review` for an implementation handoff; "
        "`kanban_request_changes` for reviewer rework; `kanban_block` only for a genuine blocker. "
        "A plain-text reply or failed tool attempt does not close the run and can cause "
        "a protocol violation. Never mutate the task after ownership moves to another run.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled", "session_called_kanban_terminal"]
