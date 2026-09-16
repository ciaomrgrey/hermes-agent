# Usage guard — INCOMPLETE CANDIDATE, DO NOT ACTIVATE

Task t_8dbb6c79, CHAT-USAGE-GUARD-001. Current run309 changes and remaining
acceptance are in CONTINUATION-HANDOFF.md, which supersedes this historical
checkpoint where they differ. This replaces the earlier partial handoff;
OWNER-CONTROL-DISPOSITION.md and GURNEY-CONTROL-DESIGN.md remain binding.
Original 2026-09-16 11:20→14:20 CEST deployment commitment is AT RISK; no reset.
Gurney's changed-design verdict is REQUIRED CORRECTIONS, not PASS or deployment.

## Source and isolation

- Shared checkout /Users/claudia/hermes/home/hermes-agent untouched.
- Candidate: /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/candidate
- Branch cody/usage-guard-t_8dbb6c79; base 6cc8c0c5f9d7a2f08971de585e584e0c901d2c28.
- First checkpoint 6b22d463e5d50e9ac65a53d3d089cda5cdf3ad36.
- Request-owned control checkpoint 24c397b12a4274993d11450a4931579638b26e67.
- Latest exact commit and dirty state: SOURCE-STATE.txt (regenerated at packaging).
- Additional detached baseline worktree at ../baseline, base6cc8c0c5f9d, for independent red reproduction.
- Candidate .venv uses repository editable install and pinned pytest8.4.2,
  pytest-asyncio1.2.0, pytest-timeout2.4.0, anthropic0.87.0 (matching operational venv).

## Executed evidence, not release claims

Read-only native source receipt usage-source-receipt.json: Anthropic seven_day33%,
Codex primary604800-second weekly32%; initialized bounded app-server corroborates
usage API. No inference requests. xAI has no equivalent source and is excluded,
not represented as zero. Fractional resets normalize to seconds but retain raw reset.

62 tests passed /0 failed across12 files at checkpoint24c397b:
checkpoint-24c397b-tests.txt contains exact command and per-file counts.
This includes deterministic windows, incidents/outbox, account-scoped holds,
source units, native Kanban/cron admission, native provider resume, turn leases,
shared transport, client lifecycle, and12 real local socket cancellation cases.

Additional post-checkpoint tests:
- concurrent-socket-receipt.txt: two simultaneous held-provider requests terminate;
  an in-flight different-provider sibling returns its real local fixture response.
  Same cached SDK template and shared inner pool; no retry/fallback request observed.
- main-anthropic-green.txt: expanded15-case HTTP/SSE/TLS/inline/async matrix passes,
  including main Anthropic blocking, streaming and cron-inline calls.

Pinned-base comparison (identical local HTTP hang-server oracle):
- socket-baseline-receipt.json: owner released=true; peer closed=false;
  provider worker alive=true. Actual imported source is baseline/agent/auxiliary_client.py.
- socket-candidate-receipt.json: owner released=true; peer closed=true;
  provider worker alive=false. Receipt captured BEFORE fixture cleanup.
- Candidate warning receipt reports tcp_force_closed=1, worker_ended=true.
- No remote cancellation acknowledgement or billing-cessation inference is made.

Expanded existing suites were NOT wholly green. expanded-regression.txt and
focused-existing-regression.txt record native Codex timeout failure and intermittent
explicit-cancel timing failures. The exact Codex timeout test was independently
reproduced RED on the detached native base: baseline-timeout-test.txt.
Do not relabel those suites as passed or waive failures silently.

## Architecture actually implemented

- Deterministic native usage adapter, strict percentage-point deltas, contiguous
  same-reset/grant segments, SQLite incidents/holds/outbox, local-time reporting.
- Hold schema (provider, account); unknown account conservatively matches any hold
  for that provider. Runtime attempts currently generally have unknown stable account;
  do not claim multiple-account selectivity. Source credential rotation resets history.
- Opt-in provider_control SQLite path; absent configuration is passthrough.
- Native turn admission plus original-provider scope prevents primary/fallback escape.
- Native Kanban gate executes before claim; cron gate skips agent jobs, leaves no_agent
  jobs untouched. Resume via hermes resume --provider PROVIDER does not clear ESTOP.
- Per-attempt SDK/httpx view uses existing shared inner HTTPTransport and owner stamp.
  Reuses _RequestClientRegistry and force_close_tcp_sockets; does not close shared SDK
  caches, shared transports, global ESTOP or gateway processes.
- Durable hold folded into the existing20ms auxiliary cancel loop. Abort sockets,
  await worker termination boundedly, discard late results; max_retries=0 on owned SDK.
- Native main blocking/streaming monitor loops and existing inline heartbeat abort
  request-owned sockets. No extra global control daemon or1s cancellation scheduler.
- Native async OpenAI creates its own AsyncClient and cancels/awaits task+close on its
  owner loop. Existing async Codex/Anthropic wrappers retain their native sync-thread
  transport but receive request-owned clients and the protected cancel edge.
- TLS pre-pool gap was reproduced RED (tls-red.txt): native walker cannot see a socket
  until start_tls returns. New request_connect_control uses the HTTP request trace
  to own a dup() of connect_tcp.complete socket until headers start. Stranger only
  shutdowns that owned duplicate; owner closes it, never the SSL BIO's descriptor.
  No pool/backend replacement. TLS oracle now passes (tls-green.txt).
- DNS/TCP before any socket exists cannot be synchronously killed. Trace gates later
  header dispatch after a hold; pending worker termination is not falsely acknowledged.

## Explicit unfinished acceptance / next implementation work

This is NOT a complete build and must NOT release final QA merely for scheduling.

1. CLI still intentionally refuses armed=true. Native receipt-validating Telegram
   adapter exists and unit tests pass, but sample/report do not yet invoke it.
   Durable retries and rendered daily/breach notices must be wired end-to-end.
2. Native no_agent scheduling/activation template and operational profile control
   configuration are not yet packaged/tested. No live cron/config modifications made.
3. Raw auxiliary stream-return paths require explicit qualified control or fail-closed
   unsupported handling; do not infer coverage from consumed-stream completion tests.
4. Complete gateway/delegation/compression/fallback/late-commit/admission race matrix,
   cross-process hold observation and stable account binding evidence remain unfinished.
5. TLS ownership extension needs Gurney review; main Anthropic inline pre-pool trace
   binding and pre-socket connect qualification require explicit follow-through.
6. Re-run expanded regressions against the final immutable commit; diagnose remaining
   candidate-vs-native timing failures. Remove obsolete cooperative-only scope.poll
   prototype rather than treating it as transport evidence.
7. Final clean commit, current patch/archive, install/rollback gate and independent
   Gurney final review t_a30da26d remain due. Hermes owns actual activation and real
   Telegram/control acceptance; neither is authorized by this handoff.

## Reproduction

From candidate:

    scripts/run_tests.sh tests/agent/test_usage_guard.py tests/agent/test_usage_guard_sources.py tests/agent/test_usage_guard_cli.py tests/agent/test_usage_guard_delivery.py tests/agent/test_provider_control.py tests/agent/test_provider_admission.py tests/agent/test_auxiliary_owned_cancel.py tests/run_agent/test_openai_client_lifecycle.py tests/agent/test_shared_http_transport.py tests/agent/test_turn_facade_lease.py tests/agent/test_cascading_interrupt_6600.py tests/run_agent/test_cross_process_turn_lease.py -j 1
    scripts/run_tests.sh tests/agent/test_provider_control_concurrency.py -j 1
    .venv/bin/python usage_guard_socket_probe.py --source ../baseline
    .venv/bin/python usage_guard_socket_probe.py --source .
    .venv/bin/python usage_guard.py status

All socket fixtures are loopback only. No quota-burning tests, live holds or real alerts.

## Rollback / installation status

Not installed. Do not copy candidate into operational checkout or enable armed=true.
Since nothing was activated, operational rollback is unnecessary. Preserve existing
Gurney monitoring and cron/quota configuration. For a future authorized installation,
removal must disable new no_agent jobs and provider_control configuration, preserve
SQLite audit/outbox evidence, and revert only this candidate's commits after independent
review; do not indiscriminately delete unrelated provider/global pause state.
