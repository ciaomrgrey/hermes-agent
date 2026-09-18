# Doorman candidate handoff — t_38cfda9d

Implementation is PARTIAL, not a claim that the error-rate repair or root acceptance is complete. No live/canonical code, profile config, cron, provider model, approval policy, gateway or update state was changed. No outbound notification was sent.

## Evidence recovered now

Native sqlite3 -readonly, read_file and search_files succeeded. The refused inline Python inventory was not rerun or repackaged. Source snapshot: base 9fa0119cb2, existing isolated branch cody/t_38cfda9d in /Users/claudia/hermes/home/kanban/boards/estate/workspaces/t_38cfda9d/candidate.

Fixed historical window: [1789641000,1789727400), ending 2026-09-18 10:30 UTC. historical-events.json contains bounded, content-free event evidence; historical-summary.txt contains SQL totals. 479 actual evaluations, 113 gate errors, 23.591% error rate. Bookkeeping, escalation receipts and reentrant pass-through are not evaluations. This differs deliberately from the intake's 111/448 all-event sample and timestamp. There is NO post-fix production day.

Stored diagnostic causes: 20 auxiliary timeouts, 7 content-filtered responses, 2 invalid responses, 84 older rows without diagnostics. recover-errors.sql additionally reads existing rotated agent logs, returning only IDs/classes. recovered-errors.csv recovers 51 outer TimeoutExpired and 18 child-exit records; 10 timestamp matches are unresolved and 5 old rows have no matching timestamp in the inspected logs. Child exit is not a root cause: stderr/response was not preserved. Do not label those records content_filter merely because a later sampled provider response was filtered. No retry or provider routing around refusals was introduced. No model switch or arbitrary timeout increase was made.

This establishes real timeout failures in the old record, not merely profile concentration. It does NOT establish whether each old timeout was startup, auxiliary routing, provider latency or malformed response processing. The old 84 rows cannot all be assigned detailed provider causes from the retained record. Existing diagnostics already identify later causes; candidate adds stable nonempty reason hashes, independent of elapsed duration, without exception text or answer leakage.

## False alarm diagnosis

Plutus messages5930–5934 show worker terminal delivery followed by an assistant-role mirror in its Slack session: message5933 at1789724685.88437 has null finish_reason/display_kind. It is not a new agent final. gateway/mirror.py discarded mirror provenance when appending SQLite. The health checker treats any persisted assistant text as a gated final, permanently retains the cursor before this unmatched mirror, then calls send every five minutes. Generalist job620736c443b6 last_run12:22:10 shows confirmed Telegram receipts4634/4635 for generalist/plutus; prior bot transport was unavailable. Receipt success did not stop the next send.

Plutus actual worker final5955 at1789724780.09828 has gate event442 at1789724797.19179. This proves workers DO invoke the gate; the live checker's blanket kanban/cron/oneshot exclusion is not copied into this candidate. The checked-in checker also allowed only +2 seconds, whereas the installed checker uses symmetric tolerance. Candidate retains symmetric bounded timing, with a recorded timing regression. Zero open cards is not used as any safety signal.

Candidate fixes:
- Persist delivery_mirror display provenance through the real native mirror-to-SQLite path. Exclude only that provenance from liveness and recent-reply checks. Null finish_reason alone, source labels, magic text prefixes, and idle wall time are not exemption authority.
- Persist confirmed alarm state in the EXISTING health SQLite DB; suppress identical confirmed sent/delivered incidents across checker processes. Failed/unverified/queued delivery is still checked/retried; recovery clears confirmation and re-arms. Health status remains unhealthy while duplicate notification is suppressed.
- Add cause hashes and evaluation-based, time-bounded error metrics while preserving existing claim-rate fields. No safety block/continuation policy or before_turn_end seam changed.

## Verification

All tests use scripts/run_tests.sh with HERMES_PYTHON=/Users/claudia/hermes/home/hermes-agent/venv/bin/python. Temporary isolated homes, actual native mirror writes, fake transport receipts, no paid/provider traffic.

RED logs in parent workspace: red-incidents.log (2 failures: lost mirror provenance and repeat sends), red-accounting.log (2 failures: empty reason hashes and no window metrics), red-worker-timing.log (recorded +17s event rejected), red-postupdate.log (mirror counted as active turn).

GREEN: full-tests.log: 12 files, 43 tests passed, 0 failed. Command:
HERMES_PYTHON=/Users/claudia/hermes/home/hermes-agent/venv/bin/python scripts/run_tests.sh tests/plugins/test_completion_gate*.py tests/agent/test_completion_gate_loop.py tests/gateway/test_mirror*.py

git diff --check passes. These are local implementation/regression tests, not a live release-tag survival test or post-fix efficacy evidence.

## Remaining work / release holds

Gurney: independently review this partial candidate, especially provenance persistence, confirmation semantics and evaluation denominator. Do NOT approve root efficacy merely from replay. John: activation and existing root t_6397391f acceptance remain yours. Candidate includes a core gateway/mirror.py change: needs an explicitly authorized upstream PR-backed exception or stock upstream release containing equivalent provenance. No PR published. Native before_turn_end seam is unchanged; real release-tag update survival remains outstanding.

Legacy message5933 has no durable mirror marker. This candidate cannot silently reinterpret every old unmarked assistant row: deploying it alone does not retrospectively clear that cursor. Owner must adjudicate precisely that historical mirror, retaining its evidence; do not reset all cursors or suppress all null-finish rows. No live state was modified here. The generalist historical gap was not independently adjudicated as false.

The <2% target remains unmet. Reliable diagnostics/accounting are repaired, but provider timeout/invalid-response incidence is not demonstrated reduced. Historical child stderr loss is irrecoverable; prospective data would add detailed diagnoses and the required post-fix full-day rate, not explain missing historical payloads. Root should retain this as unresolved rather than wait as a substitute for the completed historical analysis.

Remaining implementation limitations: existing timestamp-based gate correlation can match nearby same-profile turns; this candidate does not claim exact turn attribution. Concurrent independent checker processes can race between send and confirmed-state persistence; the existing single native cron lane is the tested scope. A crash after transport success but before confirmation remains an at-least-once window. Main health_check_failed exception notifications bypass incident deduplication; not implicated in observed repeated liveness_gap. Gate adapter/config/audit-unavailable errors can remain log-only; this change does not manufacture gate.db rows when DB is unavailable.

Rollback: candidate only, so no runtime rollback required. If separately authorized later, revert candidate commit(s) and restore approved checker/package. Additive confirmed_alarms table and delivery_mirror values need not be destroyed. Never roll back gate.db evidence.

## Existing job amendment proposal (NOT applied)

Inventory: generalist620736c443b6 is enabled no_agent completion-gate-health-sweep.sh every5min, deliver local, but checker performs its own Telegram fallback. Gurney0d2f45e2570b is enabled05:45 Telegram nightly review, current prompt uses cumulative metrics and incorrectly limits direct alerts to advice_failed_again. Gurneye9f72f3efbae is enabled4-hourly no_agent burn_rate_watch.py with explicit local-only/no-outward-alert hold.

For0d2f45e2570b replace only the measurement/alert paragraph:
"Use one explicit trailing86400-second window with both endpoints. First line: Doorman 24h: N turns · X% unverified (gate_error Y%) · Z blocks · profiles>5% error: … . N means gate evaluations (deliver/block/fail_open/gate_error); exclude reentrant_pass and advice/escalation receipt rows. X means evaluations with any unverified claim or gate_error / N, not cumulative claim percentage. Report zero-evaluation rates N/A. Retain unknown causes as unknown. If Y>5% or the hook is missing/disabled, put ALERT and that fact first, overriding the old advice_failed_again-only restriction. Recommendations only; never alter config or deploy. Missing-hook intervals and test events must remain visible, not excised to make the rate green. State incomplete coverage separately."

For e9f72f3efbae: because it is no_agent, changing prompt alone cannot run a measurement. Propose a small extension of the existing burn_rate_watch.py to consume the same read-only gate.db window and hook-status evidence, emitting structured local breach evidence for Gurney/John. Do not add a job or claim a local-only sensor provides an immediate Lars alert. The root's requested immediate outward alert conflicts with that existing explicit local-only hold; John must reconcile delivery authority before activation. No cross-profile script/cron change made by Cody. Existing five-minute checker already offers a faster hook-health carrier; do not create another monitor.
