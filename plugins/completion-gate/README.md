# Completion gate (opt-in)

A general plugin discovered from the shared install's `plugins/completion-gate/`
by CLI and gateway profiles. No estate configuration is changed by installation.
Requires the additive Python `before_turn_end` hook documented in
`website/docs/user-guide/features/hooks.md`; copying only this plugin into an older
runtime is not sufficient.

## Evidence and limits

Only the written candidate answer is submitted to native `auxiliary.completion_gate`
via `agent.auxiliary_client.call_llm`. A bounded child process prevents provider
fallback/retry from exceeding the extraction deadline. Strict JSON is mandatory;
malformed output, duplicate keys, unknown evidence, and exceptions fail open.

Read-only checker children reproduce file existence/nonempty, HTTP HEAD status
(no redirects/credentials/query), TCP connection, process existence, JSON/YAML
config equality, exact SQLite/kanban row presence, native terminal-result exit
code, and cron ID presence. The extractor never supplies executable commands.
File contents/quality, stronger claims, reports from other agents, and references
not explicitly supplied in the answer are unverified, not reproduced. This is not
a truth guarantee: extraction can omit/misclassify claims. Live extraction quality
and unverified rate must be accepted by the owner.

Defaults under `plugins.entries.completion-gate.settings`:

| Setting | Default |
|---|---|
| enabled | false |
| max_blocks | 2 |
| check_timeout | 10 seconds per child |
| extract_timeout | 10 seconds |
| total_timeout | 20 seconds for extraction/checking |
| max_claims | 20 |
| max_answer_chars | 32000 |
| db_path | default Hermes root / state/completion-gate/gate.db |

The estate default resolves to `/Users/claudia/hermes/home/state/completion-gate/gate.db`.
Keep `plugins.hook_callback_timeout` at its native 30-second default (or greater
than the configured evidence budget); timeout abandons the callback fail-open.
SQLite has a one-second lock timeout. Native hook timeout also bounds routing.

A block is reserved atomically in append-only SQLite. Counters are scoped by
profile and `HERMES_KANBAN_TASK`, falling back to session ID (then task ID), so
restart cannot reset the task ceiling. Without Kanban, the ceiling deliberately
covers the entire session. Same normalized claim/mismatch with exact reference
cannot block twice. Only one block is honored per turn; its rework iteration is
passed through, not independently certified. A later turn can reproduce the fix.
Concurrent callbacks cannot exceed the ceiling. Recursive calls bypass extraction.

SQLite stores claim hashes, whitelisted artefact kinds, deterministic mismatch
codes, verdicts, reason hashes, block counts and routing/advice markers. Raw
answers, references, config values and exception messages are not stored; the
hash permits correlation without persisting potentially secret claim text.
Mismatch details exist only in the transient model repair request. Both rejected
answer and repair request are excluded from durable conversation history.

Unverified rate uses logged claim records as denominator; an extraction/crash
failure contributes one unverified sentinel, not an invented claim count.
Rework bypasses have no claim records and are separately visible as
`reentrant_pass`. Metrics are evidence coverage, not percentage factual accuracy.

## Escalation and explicit advice markers

Worker ceiling/repeated reason => generalist inbound; generalist => gurney inbound.
Native `tools.bot_live_delivery` writes an at-most-once queued receipt to a live
owner. The exact receipt is read back. `queued` means admitted, NOT processed.
No live owner => `escalation_unavailable`, never a fabricated send. The plugin
neither spawns an owner nor touches legacy pending-message files. Reasons are
deduplicated durably; unavailable sends are not automatically retried. Owner
must inspect these statuses and maintain live generalist/gurney inbound owners.

Gate-authored inbound messages are exempt only when they match a durable event
and its recipient exactly. Arbitrary magic prefixes do not disable the gate.

The CLI is registered through `ctx.register_cli_command`:

    hermes completion-gate metrics
    hermes completion-gate advice --profile generalist --task TASK_ID --marker advised
    hermes completion-gate advice --profile generalist --task TASK_ID --marker tried

Gurney/Hermes explicitly record advice then its attempted application. A further
false result emits `advice_failed_again` to Gurney. Only Gurney's owner-managed
monitoring may escalate that case to Lars; this plugin never sends to Lars.
Normal reporting is aggregate counts, per-profile counts and unverified rate.
The separate nightly recommendation cron is Hermes's deployment responsibility.

## Deployment (Hermes only, after Gurney acceptance)

1. Integrate the reviewed candidate commit into the shared source install using
   the estate's native update/release process. Preserve the prior release SHA.
   Do not copy core files individually or point live profiles at this worktree.
2. For each owner-approved profile, run the following with that profile's
   HERMES_HOME (preserves existing plugin enablement):

       HERMES_HOME=/absolute/profile/home hermes plugins enable completion-gate --no-allow-tool-override
       HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.db_path /Users/claudia/hermes/home/state/completion-gate/gate.db
       HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.max_blocks 2
       HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.enabled true

   Shared discovery covers present and future profiles; future profiles remain
   opt-in until configured. Do not replace `plugins.enabled` with a singleton.
3. Verify effective auxiliary routing (`auxiliary.completion_gate`, or native
   auto route), shared DB permissions and live owner availability for generalist
   and gurney. No provider credentials are added by this plugin.
4. Restart/reopen affected CLI/gateway runtimes through the native lifecycle
   owner so the new code and plugin registrations load. Read back discovery and
   effective profile config. No restart is performed by this candidate.
5. Live acceptance: ask a real turn to claim an absolute nonexistent file exists.
   Read the block event and mismatch, observe same-turn repair/corrected answer,
   and confirm rejected text was not displayed or persisted. Test a truthful
   absolute nonempty file claim on a fresh turn and confirm `reproduced` delivery.
   Exercise ceiling on one stable task with distinct missing paths over turns;
   read escalation event AND native receipt AND recipient's actual inbound turn.
   A localhost fake-provider test is not this live acceptance.
6. Owner installs Gurney's separate nightly recommendation-only cron with metrics
   and advice-cycle monitoring. Nothing here creates cron or Telegram sends.

## Kill / rollback

    HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.enabled false

This hot switch stops checking, blocking and routing on the next callback without
restart. A callback already in flight retains its original settings and bounded
deadline. Registration still defers assistant streaming; to restore incremental
text, run `hermes plugins disable completion-gate` for the profile and restart its
runtime. For code rollback, use the owner's native release rollback to the saved
SHA. Preserve gate.db: deleting it would erase persistent safety counters.

## Verification

Run from the candidate/shared repository with a qualified Python environment:

    HERMES_PYTHON=/path/to/python scripts/run_tests.sh tests/plugins/test_completion_gate.py tests/plugins/test_completion_gate_checks.py tests/plugins/test_completion_gate_integration.py tests/agent/test_turn_end_hook.py tests/run_agent/test_completion_gate_loop.py

Tests use real temporary files, SQLite, local HTTP/socket servers, native plugin
discovery, native router against a local provider, native live-owner queue receipt,
and the actual AIAgent loop/SessionDB with synthetic model responses. They do not
send estate messages, invoke real model providers, or certify live deployment.
