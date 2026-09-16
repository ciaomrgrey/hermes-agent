# Continuation checkpoint — incomplete; activation prohibited

Task t_8dbb6c79, run309. Continues f7b137989224fba795eef65d17720cdd47fc0db3 on the same isolated branch cody/usage-guard-t_8dbb6c79. Base remains6cc8c0c5f9d7a2f08971de585e584e0c901d2c28. No shared checkout edits, real sends, production holds, cron installation or authentication changes. Original11:20→14:20CEST clock unchanged;13:10 missed;13:40 owner checkpoint binding.

## Material changes

- CLI sample/report now actually drains the existing outbox through render_notice/send_notice and the native Telegram receipt validator. Delivery is explicitly opt-in via boolean delivery_enabled (default false); armed=true is STILL refused. Delivery_enabled is not an activation approval and does not qualify control. Status never sends.
- End-to-end CLI dispatch tests replace only external usage/Telegram I/O. Report delivery failure retries the same saved notice, accepted receipt suppresses later duplicate sends, and night breach hold commits BEFORE failed delivery. No real Telegram receipt claimed. At-least-once crash semantics remain explicit.
- Removed the obsolete ActiveScope.poll cooperative-only prototype and schedule argument. Scope preserves original-provider admission identity, not transport ownership.
- Found and fixed a sync auxiliary fallback admission escape: target outside configured providers formerly bypassed the original held scope. RED fallback-scope-red.txt; GREEN fallback-scope-green.txt. Active fallback-to-uncontrolled-provider cancellation is still NOT qualified.
- Inactive deploy/cron-create.json is accepted by native create_job in a temporary store. Initial paused state, no inference snapshots, local normal output, explicit Telegram failure route, resume and remove verified. This is only a scheduling record template: runtime script placement, control config and install/rollback integration remain unfinished; do not install it.

## Additional qualification

- continuation-regression.txt:28 passed/0 failed (five files) before later matrix expansion.
- native-template-test.txt:1 passed/0 failed, separate run.
- cross-process-receipt.txt:1 passed/0 failed. Spawned child owns request and socket; parent writes only durable SQLite hold. Server sees peer closure; child reports cancellation then exits0. No IPC cancellation or process kill used for the successful path.
- admission-races.txt:3 passed/0 failed. Holds before registration, after registration and at late callback return refuse/discard result and unregister/close wrapper. These are deterministic race barriers, NOT additional socket teardown evidence.
- inline-tls-qualification.txt:2 passed/0 failed. Includes actual main Anthropic cron-inline streaming TLS hang, not just its non-stream worker path.
- raw-inline-qualification.txt:16 passed/1 FAILED. Real raw returned OpenAI SSE iterator outlives create(); provider socket remains open after durable hold. This is a CURRENT candidate defect, not the pre-existing native timing failure. It must not be waived or hidden by aggregate-stream test results.
- Final checkpoint commands and full output are continuation-final-focused.txt / continuation-final-existing.txt. Counts from different runs overlap: never sum them as unique tests.

## Why not release-ready

The per-attempt wrapper lifetime ends when create() returns, whereas a raw returned stream retains an active socket and can be consumed later. The existing protected owner loop is therefore absent for that stream's remaining lifetime. Closing a shared client, silently converting streaming to aggregation, or adding a global polling daemon is NOT an acceptable shortcut. A request-owned stream-lifetime correction is required (both sync and async paths), or an owner-approved explicitly unsupported/fail-closed disposition; the latter is not automatically full contract acceptance. The failing local-server test remains enabled as the executable gate.

Also incomplete: active fallback outside configured provider set, stable multi-account bindings, full gateway/BotChat/delegation/compression admission/commit matrix, real delayed pre-socket connect qualification, bounded outbox retention and final install/runtime-wrapper integration. Source token-fingerprint rotation and unknown-account conservative holds remain as documented in HANDOFF. DNS/TCP before an owned socket exists is not synchronously cancelled; local socket close never proves remote cancellation or billing cessation.

## Reproduction

From candidate, use scripts/run_tests.sh, never operational credentials or a real inference probe:

    scripts/run_tests.sh tests/agent/test_usage_guard.py tests/agent/test_usage_guard_sources.py tests/agent/test_usage_guard_cli.py tests/agent/test_usage_guard_delivery.py tests/agent/test_usage_guard_deploy.py tests/agent/test_provider_control.py tests/agent/test_provider_control_races.py tests/agent/test_provider_admission.py tests/agent/test_auxiliary_owned_cancel.py tests/agent/test_provider_control_concurrency.py -j 1
    scripts/run_tests.sh tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_explicit_cancellation.py tests/agent/test_shared_http_transport.py -j 1

Exact commit is in CONTINUATION-SOURCE.txt; full Git bundle plus patch/evidence archive are preserved outside candidate. Nothing was installed; no production rollback is needed. Candidate commits can be discarded by abandoning this worktree, but preserve receipts/state. Do not reset the shared checkout. No personal login/consent is currently the engineering blocker. Gurney final QA t_a30da26d must remain gated until full build completion. Hermes owns operational disposition, eventual authorized deployment, real Telegram and control/liveness acceptance.
