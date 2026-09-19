"""Bounded turn-end nudges for workers without a durable lifecycle handoff.

Only the originating run can require more work; successor ownership is never
permission for an exiting worker to mutate the board.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

from agent.delegation_context import owned_kanban_task

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Only dispatcher-owned workers participate, unless explicitly disabled."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Read durable run state, never infer transition success from messages."""
    if not kanban_stop_nudge_enabled() or attempts >= max_attempts:
        return None

    tid = (task_id or owned_kanban_task() or "").strip()
    raw_run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    # Tool invocation is not proof of success, and task.status may already belong
    # to the next reviewer. Reuse the goal loop's originating-run lifecycle read.
    if raw_run_id.isascii() and raw_run_id.isdecimal() and int(raw_run_id) > 0:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing

        with connect_closing() as conn:
            status = kb.goal_run_status(conn, tid, int(raw_run_id))
        if status != "running":
            return None
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal lifecycle transition.\n\n"
        f"No terminal transition has been verified for your run of task `{tid}`. "
        "Use `kanban_show` to check your run and ownership before acting; never "
        "change a successor run. A clean exit without a lifecycle transition "
        "can cause a protocol violation.\n\n"
        "Finish the authorized deliverable and use the task's review model: "
        "`kanban_complete(summary=..., artifacts=[...])` for completed work, "
        "`kanban_request_review(summary=...)` for a same-card review handoff, "
        "`kanban_request_changes(reason=...)` for reviewer rework, or "
        "`kanban_block(reason=...)` for a genuine blocker. "
        "A failed tool call is not a transition. Do not repeat a successful handoff.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled"]
