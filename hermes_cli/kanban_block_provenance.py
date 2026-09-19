"""Read legacy block provenance without rewriting historical task counters."""
import json


def previous_worker_recurrences(conn, task_id: str, stored: int) -> int:
    """New events count claims explicitly; old synthetic runs did not.

    A claim lock disambiguates a real, same-second claim from a synthesized
    run even before the dispatcher records its PID. Missing history retains
    the guard rather than silently forgiving unknown attempts.
    """
    row = conn.execute(
        "SELECT e.payload, r.id, r.worker_pid, r.claim_lock, r.started_at, r.ended_at "
        "FROM task_events e LEFT JOIN task_runs r ON r.id = e.run_id "
        "WHERE e.task_id = ? AND e.kind IN ('blocked', 'block_loop_detected') "
        "ORDER BY e.id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None:
        return stored
    payload = json.loads(row["payload"] or "{}")
    if "worker_attempt" in payload:
        return stored
    if (row["id"] is not None and row["worker_pid"] is None
            and row["claim_lock"] is None and row["ended_at"] is not None
            and row["started_at"] == row["ended_at"]):
        return 0
    return stored
