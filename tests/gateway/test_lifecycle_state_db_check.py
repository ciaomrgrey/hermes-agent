"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit precisely because it costs ~2s on a
500MB store; a clean boot must not pay it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gateway.lifecycle_ledger import (
    check_state_db_integrity,
    get_lifecycle_sentinel_path,
    record_startup,
)

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max; never alive


def _write_sentinel(home: Path, phase: str = "running") -> None:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "phase": phase,
            "pid": _DEAD_PID,
            "start_time": 1000.0,
            "started_at": "2026-08-26T23:56:45+00:00",
        }),
        encoding="utf-8",
    )


def _make_state_db(home: Path, *, corrupt: bool) -> Path:
    """Build a real SQLite file, optionally with a genuinely torn b-tree page."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(4000)]
    )
    conn.commit()
    conn.close()
    if corrupt:
        with open(path, "r+b") as handle:
            handle.seek(4096 * 6)
            handle.write(b"\xEF" * 4096)
    return path


def _exit_diag_records(home: Path) -> list:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ── the checker itself ──────────────────────────────────────────────────────


def test_checker_passes_a_healthy_store(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    assert check_state_db_integrity(home=tmp_path) == "ok"


def test_checker_reports_a_torn_btree_page(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    verdict = check_state_db_integrity(home=tmp_path)
    assert verdict != "ok"
    assert "btreeInitPage" in verdict or "malformed" in verdict.lower()


def test_checker_tolerates_a_missing_store(tmp_path: Path) -> None:
    assert check_state_db_integrity(home=tmp_path) == "absent"


# ── wiring into the unclean-exit path ───────────────────────────────────────


def test_unclean_exit_records_the_corruption_verdict(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert evidence["state_db_integrity"] != "ok"
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] != "ok"


def test_unclean_exit_on_a_healthy_store_records_ok(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert evidence["state_db_integrity"] == "ok"


def test_clean_exit_does_not_pay_for_the_check(tmp_path: Path, monkeypatch) -> None:
    """A clean boot must not scan the store — that is the whole cost gate."""
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path, phase="exited")

    called = []
    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok"
    )
    record_startup(home=tmp_path)

    assert not called, "integrity check ran on a clean boot"


# ── startup-watchdog lease during the check (#115542) ───────────────────────


def _armed_handle(monkeypatch):
    """A real, thread-less StartupWatchdogHandle installed as the module singleton.

    ``report_startup_progress`` resolves the singleton, so the lease bookkeeping the
    check performs lands on this handle; no watchdog thread means nothing can exit pytest.
    """
    import hermes_startup_watchdog as sw

    handle = sw.StartupWatchdogHandle(timeout_s=300.0, exit_code=75)
    monkeypatch.setattr(sw, "_handle", handle)
    return handle


def test_unclean_exit_check_renews_the_startup_lease_while_sqlite_progresses(
    tmp_path: Path, monkeypatch
) -> None:
    """A 37 GB store needs ~4200 s of quick_check; one 900 s entry lease cannot cover it
    and the watchdog killed every boot with exit 75 (#115542). The check must keep renewing
    a phase-owned lease from SQLite's progress handler for as long as the PRAGMA advances,
    through the real unclean-exit entry point.
    """
    import gateway.lifecycle_ledger as ledger

    handle = _armed_handle(monkeypatch)
    # Renew on every handler tick so the renewal count is deterministic, not clock-bound.
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_LEASE_RENEW_S", 0.0)
    monkeypatch.setattr(ledger, "_INTEGRITY_CHECK_PROGRESS_OPS", 1_000)
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None and evidence["state_db_integrity"] == "ok"
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert handle._lease_count > 1, "lease was taken once at entry and never renewed"


def test_unclean_exit_check_keeps_the_verdict_contract_under_the_lease(
    tmp_path: Path, monkeypatch
) -> None:
    """The progress handler must never convert corruption into success or abort the PRAGMA:
    a torn page still yields the first complaint, and the phase is still on record."""
    handle = _armed_handle(monkeypatch)
    _make_state_db(tmp_path, corrupt=True)

    verdict = check_state_db_integrity(home=tmp_path)

    assert verdict not in ("ok", "absent") and not verdict.startswith("check-failed")
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert check_state_db_integrity(home=tmp_path / "nowhere") == "absent"


# ── deferred check: off the startup critical path (t_7de1b4a4) ──────────────
#
# Every OS reboot SIGTERMs the gateway past its graceful window, so the next boot reads an
# unclean exit; a cold-cache quick_check of the 1 GB generalist store then held platform
# connect for ~12 min (14 Nov 2026).  The gateway defers the check to a background thread.


def _pending_path(home: Path) -> Path:
    from gateway.lifecycle_ledger import get_pending_integrity_check_path

    return get_pending_integrity_check_path(home)


def test_deferred_record_startup_does_not_run_the_check(tmp_path: Path, monkeypatch) -> None:
    import gateway.lifecycle_ledger as ledger

    called = []
    monkeypatch.setattr(ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok")
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=True)

    assert evidence is not None and "state_db_integrity" not in evidence
    assert not called, "integrity check ran on the startup path"
    assert _pending_path(tmp_path).exists()
    sentinel = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text())
    assert sentinel["phase"] == "running" and sentinel["prior_unclean_exit"] is True
    assert _exit_diag_records(tmp_path) == []


def test_deferred_check_records_corruption_verdict_and_errors(tmp_path: Path, caplog) -> None:
    import logging
    from gateway.lifecycle_ledger import start_deferred_integrity_check

    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)
    record_startup(home=tmp_path, defer_integrity_check=True)

    with caplog.at_level(logging.ERROR, logger="gateway.lifecycle_ledger"):
        thread = start_deferred_integrity_check(home=tmp_path)
        assert thread is not None
        thread.join(timeout=30)

    records = _exit_diag_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tag"] == "gateway.previous_unclean_exit"
    assert records[0]["prior_pid"] == _DEAD_PID
    verdict = records[0]["state_db_integrity"]
    assert verdict not in ("ok", "absent") and not verdict.startswith("check-failed")
    assert any("FAILED integrity check" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
    assert not _pending_path(tmp_path).exists()


def test_deferred_check_holds_no_startup_lease(tmp_path: Path, monkeypatch) -> None:
    """Off the startup path the check must not counterfeit startup progress (#115542 semantics)."""
    from gateway.lifecycle_ledger import start_deferred_integrity_check

    handle = _armed_handle(monkeypatch)
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)
    record_startup(home=tmp_path, defer_integrity_check=True)
    start_deferred_integrity_check(home=tmp_path).join(timeout=30)

    assert _exit_diag_records(tmp_path)[0]["state_db_integrity"] == "ok"
    assert handle._lease_count == 0 and handle._lease_phase is None


def test_clean_boot_starts_no_deferred_check(tmp_path: Path) -> None:
    from gateway.lifecycle_ledger import start_deferred_integrity_check

    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path, phase="exited")
    record_startup(home=tmp_path, defer_integrity_check=True)

    assert start_deferred_integrity_check(home=tmp_path) is None
    assert not _pending_path(tmp_path).exists()


def test_interrupted_deferred_check_is_finished_by_the_next_boot(tmp_path: Path) -> None:
    """A life that exits cleanly before the check finishes must not erase the obligation:
    the pending record survives the clean exit and the next boot runs the check."""
    from gateway.lifecycle_ledger import mark_exited, start_deferred_integrity_check

    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)
    record_startup(home=tmp_path, defer_integrity_check=True)
    mark_exited(0, home=tmp_path)  # check never started in that life

    assert record_startup(home=tmp_path, defer_integrity_check=True) is None  # clean boot
    start_deferred_integrity_check(home=tmp_path).join(timeout=30)

    records = _exit_diag_records(tmp_path)
    assert len(records) == 1 and records[0]["state_db_integrity"] != "ok"
    assert records[0]["prior_pid"] == _DEAD_PID
