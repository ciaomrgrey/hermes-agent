# CHAT-USAGE-GUARD-001 — tested local candidate handoff

Builder task t_8dbb6c79; sole writer Cody. Implementation phase complete for independent Gurney review t_a30da26d, NOT deployed or approved for activation. Original 16 September 11:20–14:20 CEST live commitment MISSED. No replacement deadline.

Source commit: 863c6185cc6c7b9582c637780a7c187894b7bafe.
Base: 6cc8c0c5f9d7a2f08971de585e584e0c901d2c28.
Branch: cody/usage-guard-t_8dbb6c79.
Worktree: /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/candidate.
The following documentation-only commit records this handoff. Earlier HANDOFF/CONTINUATION/LIFETIME/C3-BUILDER handoffs are historical checkpoints, superseded only for their unfinished builder gates by this document. Owner dispositions remain binding.

## Outcome and architecture

Deterministic sample/report/status CLI, canonical SQLite history/holds/outbox, strict configurable percentage-point rolling-six-hour threshold, local night interval, daily rolling-peak report, persistent receipt-based delivery retry. No model-based watcher or extra daemon. Inactive native no_agent cron template invokes the same installed interpreter independently of held agent providers.

Provider-only control uses native request-owned SDK views, shared transport ownership and cancellation registry. Sync/async, HTTP/SSE and raw caller-idle streams retain ownership until actual completion/close. Shared clients/pools are not closed. The original provider remains binding through fallback. Canonical SQLite holds are re-read by independent processes. Holds never auto-expire; native explicit provider resume is separate from global ESTOP.

Native completion persistence now reserves canonical control SQLite before the native session transaction, checks applicable providers inside the reservation, and retains it through actual commit. Hold-first rejects uncommitted assistant output; commit-first preserves legitimate history. Local writer waiting, retry sleeps, checkpoints, hooks, compression and network work are outside the reservation. Guarded attempts temporarily set native session busy_timeout=0 under its connection lock, then restore the original value on every path: SQLite's own one-second busy wait must not retain the global reservation. Existing native retry/backoff remains unchanged.

Rejected results preserve pending input and durable prior state, scrub uncommitted completion from live/returned snapshots, reject recovery-JSONL success, and do not claim durability without native persisted markers. Trajectories and later hooks observe the rejection boundary. Nonquiet CLI/Kanban workers now exit1 for provider-held/persistence-failed turns; ordinary user cancellation behavior is unchanged. Expected held auto-title calls no longer escape the daemon as tracebacks.

## Exact verification

From candidate/:

    scripts/run_tests.sh tests/run_agent/test_*persist*.py tests/agent/test_*flush*.py tests/agent/test_turn_finalizer*.py tests/test_state_db_write_durability.py tests/agent/test_session_persistence*.py tests/agent/test_provider_*.py tests/agent/test_usage_guard*.py tests/agent/test_auxiliary_owned_cancel.py tests/agent/test_auxiliary_stream_lifetime.py tests/agent/test_title_generator.py tests/cli/test_single_query_session_finalize.py tests/cli/test_single_query_clarify.py -j 3

Final receipt final-qualified-gate.txt: exit0, 258 passed / 0 failed, 43 files, 49.4s, no flaky-file warning. This is the qualified targeted native regression set, NOT the repository's entire test suite. git diff --check passed. Counts from older overlapping runs are not added.

Evidence boundaries:
- test_provider_cli_ingress.py: 14 real subprocess cases, admission + active loopback wire for direct CLI, canonical Bot Chat arguments, native cron run_job, gateway TurnRunner with real AIAgent, native delegate child runner, native Kanban _default_spawn, and actual ContextCompressor. Active cases require exactly one inference request and server-observed peer close within two seconds. No production provider inference or delivery. Native DBs retain pending input with no new assistant row; Kanban task body/status remains unchanged. Compressor checks caller snapshot unchanged, not a fabricated DB receipt. Gateway shell has no delivery adapter; delegate parent is a minimal shell around the actual child runner. These are native host boundaries, not a live gateway deployment or every provider-by-host Cartesian product.
- test_provider_commit_order/recovery/contention/late_persist: actual SQLite same/cross-process order; crash immediately after real commit; reopen, native explicit resume, repeated flush/deduplication; missing/malformed/locked control; aborted/replaced native DB; fallback; compressed snapshots; downstream hook checks; configured sibling and real busy retry. Synthetic answers only.
- test_auxiliary_owned_cancel/stream_lifetime plus provider_tcp_timeout: real loopback HTTP/SSE/TLS and subprocess hold; sync/async, raw idle stream lifetime and sibling pool isolation. TCP-TIMEOUT-RECEIPT.json records native numeric-address connect=15s, twice, no fixture release, owner cancelled and worker naturally drained. Event-delayed presocket test is ONLY no-late-dispatch evidence.
- test_usage_guard_native_install: actual editable installation into disposable venv, reused installed test dependencies (no fresh dependency-resolution claim); native config commands; paused cron creation then activation ONLY in temporary store; two fresh native script children while Codex hold persists; installed module origin/effective home checked; removal and de-wiring rollback retain SQLite. Provider values explicitly synthetic, delivery disabled. A parent-only PYTHONPATH attempt failed; installation fixes the test's missing deployment prerequisite, not a hidden runtime path shim.
- Numerical/source/delivery/retention suites cover strict threshold, off-clock peak, percent units, resets/jitter/gaps/partial history, night/DST, durable original incident retry/dedupe, daily reports and saturation. Delivery adapter tests use fake transport receipts, not real Telegram delivery.

Preserved negatives: late-persist and trajectory/recovery/output-hook RED receipts; native-busy-boundary-red.txt; active-cli-bot-diagnostic.txt (title daemon); kanban-native-first.txt (nonquiet exit0). Earlier cold child-start flake has explicit readiness handshake; first broad compression/host command exceeded its 180s outer budget (compression-native-first.txt), with no surviving matching processes at inspection. Narrow rerun and final full targeted set pass without retry. Baseline Codex timing failure remains historical evidence, not claimed repaired.

## Sources and account scope

source-stable-live-receipt.json is a READ-ONLY probe at 2026-09-16T12:38:16Z, not a current quota claim: Anthropic seven_day=34.0%, duration604800s; Codex rate_limit.primary_window=40.0%, duration604800s. Initialized bounded app-server independently agrees40%/10080min and was closed. Binding is weekly duration, never primary/secondary labels. Model-specific codex_bengalfox is excluded. xai-oauth is unsupported, never zero.

grant-inventory-receipt.json records one stable account+organization Anthropic grant and one stable Codex account grant across inspected native profiles, including Chas/default. Fingerprints only; no credential values. Rotation does not invent a new grant; multiple distinct grants are refused instead of merged. Hermes must revalidate actual operational inventory at release. Saved-store readers do not refresh/select/heal auth.

## Source map

Deterministic implementation: usage_guard.py; agent/usage_guard.py, usage_guard_sources.py, usage_guard_delivery.py; config.json; deploy/cron-create.json and usage-guard-tick.py.
Control/native integration: agent/provider_control.py, auxiliary_control.py, auxiliary_stream_control.py, request_connect_control.py, auxiliary_client.py, agent_runtime_helpers.py, chat_completion_helpers.py, chat_completion_nonstream.py, chat_completion_stream_monitor.py, interrupt_control.py, turn_facade.py, turn_finalizer.py, session_persistence.py, title_generator.py; hermes_state.py, hermes_state_messages.py; cli.py; hermes_cli/cli_chat_turn_mixin.py, kanban_db_dispatch.py, subcommands/pause.py; cron/scheduler.py.
Native fixtures/regressions: tests/agent/test_provider_*.py, test_usage_guard*.py, test_auxiliary_owned_cancel.py, test_auxiliary_stream_lifetime.py; provider_gateway_fixture.py, provider_kanban_fixture.py, provider_compression_fixture.py. Unrelated preexisting test files selected by the globs are not claimed as newly authored.

## Installation/rollback contract — NOT authorization to execute live

Hermes owns deployment after Gurney verdict and explicit release disposition. Use the reviewed commit in the interpreter that launches each runtime, not a source-only file copy or assumed PYTHONPATH. The disposable gate exercises this install shape:

    <runtime-python> -m pip install --no-deps --no-build-isolation -e <reviewed-candidate>

Using each intended native profile home, stage config.json under usage-guard/ and the script under scripts/. Keep armed=false and delivery_enabled=false. Initialize/read the canonical state without a provider fetch:

    <runtime-python> <reviewed-candidate>/usage_guard.py status --config <owner-home>/usage-guard/config.json

For every controlled runtime profile, native config commands must point to ONE same canonical DB:

    hermes -p <profile> config set provider_control.database <owner-home>/usage-guard/state.sqlite3
    hermes -p <profile> config set provider_control.providers '["anthropic", "openai-codex"]'

Under the intended owner profile, use native cron.jobs.create_job(**json.load(open(<reviewed-candidate>/deploy/cron-create.json))) and preserve the returned job ID. Template is paused, no_agent=true, deliver=local; failure target is explicit Telegram. Do not resume this job or flip armed/delivery as part of builder handoff. No invented command alias is required: the actual create/resume/remove APIs are exercised by the install test.

Rollback in the disposable gate removes only its created native cron job with cron.jobs.remove_job(job_id), de-wires provider_control.providers with native config set '[]', and retains control/history/pending outbox. Live rollback must restore recorded preinstall config/code and be owner-orchestrated: de-wiring enforcement while real holds exist is itself a release decision. No live rollback is currently necessary. Explicit operator resume, if later authorized: hermes -p <configured-profile> resume --provider anthropic (or openai-codex); this does not remove ESTOP. Never auto-resume on update, restart or morning boundary.

## Residual release risks and next owner

- DNS/getaddrinfo is unbounded and not immediately cancellable. Native finite TCP drain is the owner's accepted refinement, NOT a universal15s termination guarantee. Local peer closure is not proof of remote provider billing cancellation.
- Pending original alerts are lossless, therefore total storage cannot remain finite during an indefinite outage with unlimited new incidents. Delivered history is pruned; saturation is explicit. Do not silently drop originals or claim this impossibility solved. Owner must disposition the operational risk.
- Account inventory, effective configuration across all live processes, Telegram delivery/readback, live provider isolation and no_agent sampler/report/outbox liveness require Hermes's release acceptance after independent review. Nothing is running live from this candidate.
- Independent Gurney QA is still required; this is builder verification, not its verdict. Release ownership must be surfaced to Hermes when t_a30da26d finishes. No Lars credential/action is currently required for the internal review.

Archive/attachment/introspection/notification refusals remain intact: no new bundle or attachment; complete source and receipts are durably committed in this existing worktree. Historical untracked receipts/state/temp directories remain preserved, not part of the deployment payload. Zero-byte stale Git index locks with no lsof/pgrep owner were preserved as index.lock.stale-152851 and index.lock.stale-161736, not silently deleted. No shared-checkout source, production config/cron/auth, Gurney job or protected pending-message file was edited.
