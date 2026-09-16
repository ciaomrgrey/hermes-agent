# C3 continuation checkpoint — NOT full build or release

Task t_8dbb6c79; sole writer Cody. Branch cody/usage-guard-t_8dbb6c79 in /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/candidate. Tested source checkpoint 12dd711a6f521c1c2fb2b27be5cf6712f0d80286. Original 16 September 11:20–14:20 CEST live commitment MISSED. No new budget or completion forecast.

## Stop finding

OWNER-C3-DISPOSITION.md line24 requires STOP/escalate if commit escapes hold. New test tests/agent/test_provider_late_persist.py fails: a durable hold inserted at native finalizer cleanup, before transcript persistence, does not prevent native SQLite flush committing assistant content `late answer`. The outer controlled_turn returns interrupted=true/completed=false afterward, but too late to prevent durable commit. It also draws returned messages from the now-contaminated session snapshot. This is actual native finalizer + native SQLite flush with a minimal existing agent shell and synthetic response; it is NOT a real provider or full end-to-end request. No quota consumed.

Root: agent/turn_finalizer.py calls _persist_session at486 before controlled_turn checks scope after function return (provider_control.py94). Transport no-late-dispatch is distinct from turn commit atomicity. No transport layer or finalizer fix added following this RED. Recommend owner authorize a bounded native finalization/commit correction, not further transport work, then require this gate and remaining actual route/install gates. The exact transaction/hold ordering must be decided explicitly; a check after persistence cannot repair it.

## Work preserved

- 2bf4bef8fd3: native15s single numeric-address TCP drain twice, zero retry, no forced fixture release, shared-pool sibling healthy. TCP-TIMEOUT-RECEIPT.json; existing C3 receipt unchanged. DNS remains unbounded/unproved.
- 7f0c53e211d: saved native credential snapshots without refresh/select/heal; stable Anthropic account+organization identity and Codex account identity. Sanitized live estate inventory and rotation tests. Distinct grants refused rather than merged; xAI unsupported.
- Retention changes: delivered history pruning, active incident dedupe preserved, pending originals retained; pending saturation exposed. Indefinite outage cannot provide lossless unlimited incidents and finite total storage simultaneously; release finding remains.
- 12dd711a6f5: turn-boundary hold becomes native interrupted result instead of BaseException escaping host worker. Held CLI/Bot Chat input persisted through native message flush; hold diagnostic is not automatically requeued as a fresh prompt. Test for post-call result withholding added, but new native-finalizer test establishes it is NOT a durable-commit guarantee.

## Verification (actual commands/results)

Run from candidate/ using scripts/run_tests.sh; every invocation -j 1.

1. tests/agent/test_provider_cli_ingress.py tests/agent/test_provider_host_results.py -> cli-ingress-green.txt: exit0. Actual CLI and canonical BOT_CHAT_TURN_ARGS subprocesses, temporary estate/config, local server; user input survives process exit; no inference POST or assistant row. Native /api/show metadata probing is allowed, not inference.
2. tests/agent/test_provider_host_results.py tests/agent/test_provider_cli_ingress.py tests/agent/test_provider_control.py -> host-qualified.txt: 13 passed,0 failed,3 files.
3. tests/agent/test_provider_control.py tests/agent/test_provider_host_results.py tests/agent/test_provider_cli_ingress.py tests/agent/test_provider_admission.py tests/agent/test_usage_guard.py tests/agent/test_usage_guard_cli.py tests/agent/test_usage_guard_sources.py tests/agent/test_usage_guard_identity.py tests/agent/test_usage_guard_retention.py tests/agent/test_usage_guard_delivery.py tests/agent/test_usage_guard_deploy.py -> c3-qualified-checkpoint.txt: 37 passed,0 failed,11 files,6.6s, exit0; git diff --check exit0. Counts overlap earlier runs; not aggregated.
4. tests/agent/test_provider_late_persist.py -> late-persist-red.txt: 0 passed,1 failed,exit1. Failure is the expected forbidden assistant row, not import/setup error. Deliberately committed failing acceptance oracle, not xfailed/waived.

## Remaining mandatory gates

Native finalizer/SQLite no-late-commit and resume; complete actual gateway/Bot Chat/cron/Kanban/delegation/compression/CLI active-route matrix, not just common seam/admission checks. Disposable installation/rollback/effective config across runtime must still be exercised; existing native cron template tests are not that acceptance. Final broad regressions and independent Gurney QA remain. Source inventory's operational release acceptance, DNS risk and outbox saturation need explicit owner disposition.

## Install / rollback / boundaries

NOT install-ready. Existing deploy/ templates remain inactive, armed=false, delivery_enabled=false. Do not install this checkpoint or release gated QA. Hermes owns later authorized deployment/real Telegram/isolation/guard liveness. No production send, hold, cron, auth or Gurney job changes. No shared checkout changes. Archive/introspection/notification refusals retained: no new bundle/archive/attachment claimed.

Rollback is local candidate-only: preserve these commits/receipts; owner may discard the isolated candidate or revert selected commits after explicit review. No live rollback required because nothing deployed. Existing untracked historical receipts/temp fixtures/state remain preserved; no destructive cleanup. No credential/password or Lars action is required for this engineering stop.
