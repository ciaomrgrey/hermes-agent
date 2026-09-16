# Run311 lifetime checkpoint — full contract INCOMPLETE, activation HELD

CHAT-USAGE-GUARD-001, t_8dbb6c79. This supersedes CONTINUATION-HANDOFF.md only for the changes/evidence below; original contract and owner dispositions remain binding. Sole Cody writer, isolated candidate worktree. No shared checkout changes, deployment, real alerts/holds, cron activation, auth changes or quota-burning inference. The original16Sep11:20→14:20CEST commitment is unchanged;13:10/13:40 candidate milestones missed. The bounded14:00 raw-stream lifetime gate passed before14:00. That is not the full candidate, review or deployed-user outcome.

## Immutable source

Worktree: /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/candidate
Branch: cody/usage-guard-t_8dbb6c79
Base:6cc8c0c5f9d7a2f08971de585e584e0c901d2c28
Resumed:226285bb36a016b3c2e3eca76204431bfd9b99ec
Lifetime implementation:98902b61d7f81c2f65f2689ded19448547f69b9a
Final tested code:103c12592ece2638648dcc04665622e3f27695e5
Subsequent commit contains this handoff/receipts only. Tracked source was clean at final execution; untracked historical receipts and local SQLite remain intentionally preserved. No new archive/bundle or checksum claim; previous security refusals remain binding. The execute_code discovery helper was also refused in this run, not rerouted through arbitrary Python.

## What changed and why

1. Raw returned OpenAI streams retain the request-owned SDK/httpx wrapper and registry until EOF, explicit close, parse error or cancellation. A per-request lifetime observer checks the captured cancellation decision during caller-idle time as well as blocked reads. It is not a global control scheduler. Sync observer performs owned-socket shutdown only; full wrapper close is serialized after the stream reader unwinds. Explicit close can wake a concurrently blocked reader. Async lifetime uses an owning-loop task, cancellation and close. No shared SDK/cache/pool closure.
2. The original raw-openai-sse reproducer remains enabled. Direct `_relay_sync_stream` was separately reproduced as bypassing control and now uses the same attempt helper; the Codex MoA aggregate path also enters it. Streaming remains streaming; no forced aggregation shortcut.
3. Active auxiliary fallback to an unconfigured target inherits the original controlled-provider scope, including sync/async and pre-pool trace binding. Previously only next admission was protected. Loopback hold-after-dispatch fixtures reproduced the active escape RED, then passed.
4. Pending pre-socket workers remain in the active-attempt table after the calling owner returns. A new delayed-connect fixture found the previous early-unregister defect RED. The record is removed when the actual worker finishes; it is not falsely called terminated.
5. Codex usage source now reuses native `codex_cloudflare_headers` account extraction from the current OAuth token. Same-account rotations retain the grant fingerprint; singleton/token identity conflicts refuse the request. Only synthetic source tests qualify this improvement; it has not been re-probed on operational credentials. Anthropic still has credential-fingerprint identity and starts partial coverage on rotation. Runtime unknown account remains conservative across that provider's holds.
6. `deploy/usage-guard-tick.py` is the inactive native no_agent entry. It runs the real CLI against the selected profile's `usage-guard/config.json` and state directory. Its integration test replaces external usage fetch only and exercises real CLI/SQLite, alongside the native paused cron create/resume/remove test. No installed runtime/wrapper claim.

Changed implementation files this run:
- agent/auxiliary_client.py
- agent/auxiliary_control.py
- agent/auxiliary_stream_control.py (new)
- agent/provider_control.py
- agent/request_connect_control.py
- agent/usage_guard_sources.py
- deploy/usage-guard-tick.py (new)
Tests: test_auxiliary_owned_cancel.py, test_auxiliary_stream_lifetime.py (new), test_provider_presocket.py (new), test_usage_guard_sources.py, test_usage_guard_deploy.py.

## Exact test commands / checked receipts

From the candidate worktree, using its native test runner and isolated temporary homes:

    scripts/run_tests.sh tests/agent/test_usage_guard.py tests/agent/test_usage_guard_sources.py tests/agent/test_usage_guard_cli.py tests/agent/test_usage_guard_delivery.py tests/agent/test_usage_guard_deploy.py tests/agent/test_provider_control.py tests/agent/test_provider_control_races.py tests/agent/test_provider_admission.py tests/agent/test_auxiliary_owned_cancel.py tests/agent/test_auxiliary_stream_lifetime.py tests/agent/test_provider_presocket.py tests/agent/test_provider_control_concurrency.py -j 1

lifetime-final-focused.txt: exit0,66passed/0failed,12files, no flaky-file warning,31.6seconds. Source103c12592ec.

    scripts/run_tests.sh tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_explicit_cancellation.py tests/agent/test_shared_http_transport.py tests/run_agent/test_openai_client_lifecycle.py tests/agent/test_turn_facade_lease.py tests/agent/test_cascading_interrupt_6600.py tests/run_agent/test_cross_process_turn_lease.py -j 1

lifetime-final-native.txt: exit0,247passed/0failed,7files, no flaky-file warning,17.5seconds. Source103c12592ec. Do NOT erase lifetime-existing-regression.txt: earlier source98902 run had223passed/1failed (native Codex total-timeout test, already independently RED on pinned base in baseline-timeout-test.txt). Latest passing run does not establish that timing flake is fixed.

Other receipts are overlapping subsets, not additional unique totals:
- lifetime-red.txt: original raw sync socket remains open,0pass/1fail.
- lifetime-idle-red.txt: sync idle plus async idle/read failures,0pass/3fail.
- lifetime-direct-red.txt: direct raw route bypass,0pass/1fail.
- lifetime-close-red.txt: concurrent explicit close blocked behind reader,0pass/1fail.
- lifetime-qualified.txt:45pass/0fail at the14:00 checkpoint.
- active-fallback-red.txt / active-fallback-green.txt:2fail then2pass.
- presocket-receipt.txt: initial setup-timeout flake, passed on retry. Preserved, not called pristine.
- presocket-qualified.txt: setup bound corrected; cancellation/peer-close bounds unchanged.
- presocket-registry-red.txt / presocket-registry-green.txt: actual pending-attempt early-unregister defect, then corrected.
- claim-red.txt / claim-green.txt: native account binding across rotation and conflict refusal.
- wrapper-red.txt / wrapper-green.txt: missing no_agent entry, then real CLI/temp-state integration.
- git diff --check: exit0. No production lint/typecheck/full39k-test-suite claim.

## Acceptance limits that still block full completion

A. Pre-socket DNS/TCP connect cannot be synchronously cancelled by this reviewed request-local seam. The real delayed-connect oracle proves calling owner released while provider worker remained pending: `tcp_force_closed=0`, `worker_ended=false`. When the delayed native connect resumes, server receives an empty connection, NO HTTP bytes; worker then ends and wrapper closes. This is no-late-dispatch evidence, NOT all-phase worker termination or remote cancellation. The pending attempt is now retained visibly in-process. No blanket kill/private transport rewrite was attempted. Owner must disposition this limit without silently waiving full acceptance.

B. Complete native surface/late-commit qualification is still missing: gateway/BotChat/delegation/compression/direct-CLI execution paths have not all been independently exercised end-to-end under holds. Current tests prove the shared turn facade, native cron/Kanban entry gates, main/aux transport paths, races, cross-process holds, active fallback and protected cancellation. Those tests must not be relabelled as every named caller surface. Resumability/error propagation at every host surface remains unqualified.

C. Stable estate-wide grant inventory is incomplete. Codex now consumes the native token account claim, with synthetic rotation/conflict coverage, but no new operational account receipt. Anthropic rotation still splits history rather than having a stable native account binding. Unknown runtime identities conservatively match any hold for the provider; multi-account selective cancellation is not claimed.

D. Outbox has durable retries/dedupe and at-least-once crash semantics, but delivered/failed notice storage is not yet bounded. Pending notices cannot simply be aged away without violating original-breach preservation. No retention waiver or silent deletion was introduced.

E. Native cron record and tick wrapper are tested only in temporary fixtures. Complete install/rollback execution against a disposable runtime estate and effective provider-control configuration propagation remain unfinished. `armed=true` remains refused and `delivery_enabled=false` remains default. Do not install or activate this candidate.

F. Gurney independent final QA t_a30da26d remains gated on the incomplete build. Source/test checkpoint is not permission to release it. Hermes retains deployment, actual Telegram receipt and provider-isolation/guard-liveness acceptance. No personal credential/consent boundary has been established; this is owner engineering/scope disposition, not Lars homework.

## Preservation / rollback / next safe commands

No production installation occurred, so there is no production rollback to run. Preserve candidate source, receipts and state; abandon/revert only candidate commits if owner chooses replacement. Never reset the shared checkout or touch existing Gurney jobs/auth/protected files. A future reviewed installation must place state outside code, install the no_agent script using native facilities, explicitly configure provider_control at the canonical root and preserve the exact created job ID for removal. Those actions are not qualified or authorized by this document.

Safe next commands are the exact two test commands above and `.venv/bin/python usage_guard.py status` (no send). Owner must choose adopt/simplify/re-route for remaining limits and original14:20 obligation; no automatic deadline renewal, unsupported-path waiver, partial completion or invented live outcome.
