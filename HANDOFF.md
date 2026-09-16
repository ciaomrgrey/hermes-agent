# Usage guard — PARTIAL CANDIDATE / DO NOT ACTIVATE

Task t_8dbb6c79; owner contract CHAT-USAGE-GUARD-001. Original start 2026-09-16 11:20 CEST; original live commitment 14:20 CEST unchanged. This artifact is NOT a completed implementation, not independent-review approval, and not a deployed guard.

## Actual outcome

The deterministic measurement/report core and a bounded native-control prototype work in the isolated candidate. Native source probes succeeded without inference. A load-bearing preflight assumption failed: native auxiliary cancellation can return to the owner while the provider attempt continues. Owner gate t_624014e5 is executing with Hermes/generalist. The builder must not release the downstream Gurney review as an acceptance-ready build until this prerequisite and the remaining integrations are complete.

No production files, existing cron jobs, auth stores, Telegram destinations or provider pauses were changed. No real Telegram messages were sent. No quota-burning validation requests were made. All synthetic fixtures are marked in tests; local callback cancellation probe is NOT real provider billing evidence.

## Native gap reproduced, not assumed

Run `.venv/bin/python usage_guard_control_probe.py`.

Actual output in control-gap-receipt.json:
- native_owner_cancelled: true
- provider_callback_still_running_after_owner_cancelled: true
- fixture_callback_cleaned_up: true

The probe executes the actual `_run_protected_sync_provider_call` under `aux_interrupt_protection` with a bounded local blocking callback. It sets cancellation, observes the owner leave, proves the callback is still active, then releases and joins it. No network calls. The pinned source explicitly documents shared auxiliary clients at agent/auxiliary_client.py:432–438. Closing a shared client is not a valid isolation fix. Request-owned auxiliary transport cancellation is required; an owner exit, hard_interrupt log, global ESTOP, or process kill is not proof of the required control.

## Implemented and exercised

- `usage_guard.py sample|report|status --config PATH`: bounded non-agent CLI, one file lock, local `config.json`/`state.sqlite3`; no parallel scheduler.
- Anthropic native OAuth usage GET, seven_day only, exact percentage units, malformed/null rejection, subsecond reset normalization with raw reset retained.
- Codex native usage API, duration 604800 seconds rather than misleading primary/secondary label. Native bounded app-server diagnostic initialize + account/rateLimits/read confirms the same subscription weekly primary.
- Every sampled endpoint rolling delta, contiguous same-grant/reset monotone segments; missing samples/resets/corrections become partial, not green. A single sample yields null, not fabricated zero burn. No interpolation.
- Strict threshold, SQLite atomic sample/incident/outbox/hold updates, unchanged-incident dedupe, durable failed-delivery retry exercised using a synthetic transport receipt. No exactly-once promise: transport acceptance before receipt commit can duplicate an incident after crash.
- Zurich night boundaries and both DST repeated-hour offsets; hold state persists and never auto-resumes.
- Daily report enqueue even without data/breach, 24h endpoints with preceding-six-hour lookback, timezone-labelled peak times, unsupported xAI, held set, explicit uncontrolled external clients.
- Opt-in generic `agent.provider_control.Policy` prototype, default disabled. AIAgent facade admission/one-second native periodic-scheduler cancellation; original-provider binding blocks fallback within the scope's checks. Native interrupt has a default-preserving `propagate_children` keyword so selectively cancelling a parent need not cancel an unrelated-provider child.

## Not implemented / not qualified — material acceptance failures

1. Request-owned auxiliary cancellation and physical-request/fallback enforcement are absent. `check_request` exists but is not wired at every provider boundary. There is no proof that all active inference stops safely.
2. Gateway/BotChat/cron/Kanban/delegate pre-admission gates and durable deferred/resumable task reconciliation are not complete. AIAgent facade gating alone is insufficient. No native explicit-resume CLI has been added; the prototype has only a Policy.resume method.
3. Native Telegram transport and supervised no_agent cron installation are not wired; outbox delivery is tested with a fixture callback only. Reports are queued locally, never delivered.
4. Source identity currently uses conservative credential fingerprints when native account_id is absent. Both live probes used this fallback. Credential rotation starts new coverage; stable grant equivalence across all profile pools is not established. Live target inventory includes Chas, but the inventory is diagnostic, not installed targeting/enforcement.
5. Outbox retention/backoff policy and new-incident identity across resets/account changes need additional completion tests. Samples are pruned at seven days. Current detector only rearms an active incident after a fully sampled non-breaching window.
6. Full native socket cancellation, adverse transport behavior, real target/isolation/restart, scheduler liveness while Codex is held, protection against direct state-file mutation, and post-install loaded-code verification remain open.
7. Full Zurich 01:00–07:00 report fixture is not yet present: existing every-offset fixture proves that off-clock span in UTC. The report uses timezone conversion; do not overstate the tested boundary.

The CLI deliberately rejects `armed=true` with a specific release blocker. Do not remove that refusal as a substitute for implementing these gates.

## Source receipts and existing history

usage-source-receipt.json, 2026-09-16 09:45:26Z:
- Anthropic seven_day 33.0%; raw reset 1789772400.643485.
- Codex usage_api primary_window 32.0%, duration604800, reset1789805425.
- app-server rateLimits.primary 32%, windowDurationMins10080, reset1789805425; process closed=true. Model-scoped codex_bengalfox is deliberately excluded.
- xAI unsupported, not zero.

A later CLI read at09:52Z observed Codex33%; that is a real read, not injected burn. cli-sample-receipt.json predates the single-reading null correction and is retained as historical execution output. cli-report-receipt.json after correction truthfully reports unavailable peaks.

history-receipt.json read the existing Gurney state without modifying it:60 historical snapshots,59 Anthropic weekly readings, maximum gap14455.119616031647 seconds. No grant identity and no preserved Codex weekly duration. It cannot establish a continuous60-second retrospective24h rolling maximum; not imported or relabelled as exact history. No waiting was used to validate the algorithm.

## Reproduce verification

From this isolated candidate directory:

    scripts/run_tests.sh tests/agent/test_usage_guard.py tests/agent/test_usage_guard_sources.py tests/agent/test_usage_guard_cli.py tests/agent/test_provider_control.py tests/agent/test_turn_facade_lease.py tests/agent/test_cascading_interrupt_6600.py tests/run_agent/test_cross_process_turn_lease.py -j 2

Actual latest result in guard-test-receipt.txt:7 files,29 tests passed,0 failed, exit0. `git diff --check` passed. This is focused/prototype/regression evidence, not complete contract acceptance or full Hermes suite evidence. Initial RED executions were observed before each implementation slice; guard-red-single-reading.txt preserves the final discriminating RED receipt.

    .venv/bin/python usage_guard.py status
    .venv/bin/python usage_guard.py report
    .venv/bin/python usage_guard_control_probe.py

These run without inference or real alerts. `sample` performs real read-only quota GETs; it writes only local candidate state. `usage_guard_probe.py` additionally launches and closes bounded native codex app-server. No cron loop is running.

## Isolation, installation and rollback

Worktree: /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/candidate
Branch: cody/usage-guard-t_8dbb6c79
Base: 6cc8c0c5f9d (full base in SOURCE-STATE.txt).
Only this worktree is a writer. Shared engine checkout unchanged. `.venv` is task-local; installed project dependencies plus pytest8.4.2, pytest-asyncio1.2.0, pytest-timeout2.4.0. No production package environment changed.

Installation: NOT AUTHORISED/NOT READY. Do not schedule, cherry-pick into the live engine, set provider_control.database, or arm this candidate. Hermes owns eventual deployment and real Telegram/control acceptance after full implementation and Gurney review.

Reconstruction only: use the exact base commit in SOURCE-STATE.txt, create an isolated worktree, and apply candidate.patch. The changes archive contains the complete task delta plus sanitized local state/receipts; it is not a distribution of the entire pre-existing Hermes repository.

Rollback now: none in production, since nothing was installed. Retain this worktree/branch and archive for the continuation. A future activation requires an owner-controlled rollback plan that disables its new cron job, explicitly resumes held providers, and restores the prior reviewed engine without modifying existing Gurney jobs.

No personal login/consent prerequisite was encountered. The pending boundary is an owner architecture/scope disposition for a broader native transport prerequisite, not a request for Lars's credentials or permission to weaken safeguards.
