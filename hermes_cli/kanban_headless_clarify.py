"""Actionable failure for clarification from a dispatched, headless CLI."""
from contextlib import closing
import sqlite3

from hermes_cli.kanban_db import kanban_db_path
from hermes_cli.kanban_db_notify import list_notify_subs
from hermes_cli.sqlite_safe_read import connect_tracked


def kanban_clarify_unavailable(task_id: str) -> str:
    """Name only the owner's rails; reading an error must never send an ask."""
    prefix = f"Clarification not delivered: Kanban task {task_id} has no interactive user. "
    try:
        with closing(connect_tracked(
            kanban_db_path().resolve().as_uri() + "?mode=ro",
            connect_fn=sqlite3.connect, uri=True, timeout=2,
        )) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
            owner = task["assignee"] if task else None
            rails = list_notify_subs(conn, task_id, notifier_profiles=[owner]) if owner else []
        if not rails and owner:
            from hermes_cli.profiles import get_active_profile_name
            if owner == (get_active_profile_name() or "default"):
                from gateway.config import load_gateway_config
                rails = [
                    {"platform": platform.value, "chat_id": cfg.home_channel.chat_id,
                     "thread_id": cfg.home_channel.thread_id}
                    for platform, cfg in load_gateway_config().platforms.items()
                    if cfg and cfg.home_channel
                ]
    except (OSError, sqlite3.Error, ValueError) as exc:
        return prefix + f"Could not resolve the owner's notification rail ({type(exc).__name__}). Do not infer consent; record the blocker on the task."
    targets = [
        f"{rail['platform']}:{rail['chat_id']}"
        + (f" (thread {rail['thread_id']})" if rail.get("thread_id") else "")
        for rail in rails
    ]
    if targets:
        return prefix + f"Owner {owner}: ask on your configured rail: {', '.join(targets)}; then block needs_input. No question was sent and no consent was received."
    return prefix + f"Owner {owner or 'unassigned'} has no notification rail configured for this task or active profile. Configure an owner notification subscription, record the question and block needs_input. Do not infer consent."
