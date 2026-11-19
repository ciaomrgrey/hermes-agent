# Completion gate (opt-in)

A general plugin distributed OUTSIDE the Hermes source tree. Install the package
at `/Users/claudia/hermes/home/plugins/completion-gate/`; native discovery scans
each effective `$HERMES_HOME/plugins/`, so named profiles need a link from their
own `plugins/completion-gate` to that canonical package. No shared-root fallback
or plugin-specific core exception is added. No profile configuration is changed.
Requires the additive Python `before_turn_end` hook documented in
`website/docs/user-guide/features/hooks.md`; copying only this plugin into an older
runtime is not sufficient.

## Evidence and limits

Only the written candidate answer is submitted to native `auxiliary.completion_gate`
via `agent.auxiliary_client.call_llm`. A bounded child process prevents provider
fallback/retry from exceeding the extraction deadline. Strict JSON is mandatory;
malformed output, duplicate keys, unknown evidence, and exceptions fail open.
The optional Chat-receipt and actionable-inaction classes are deterministic and
submit no source message, tool result, card text, or Telegram text to that model.

### Chat decision receipts and actionable inaction (default off)

`chat_receipts_enabled` applies only to `generalist` turns from the configured
Slack channel whose source has a durable platform request ID, is no more than 15
minutes old, and whose whole text has exactly the conservative shape
`<Lars decided:|DECISION-*:> <imperative decision>; <bare solicitation>`, where the
decision is one approved verb (apply, approve, accept, merge, ship, release, deploy, defer,
unblock, proceed/go ahead with) on explicit card ids or "the approved change",
optionally "now"; reported, hypothetical or rejected wording never matches (e.g. "confirm
to me on Telegram", "let me know when it is done"). Authority attaches to the
complete request: any question, quotation, negation (including typographic
apostrophes, normalized via NFKC), withdrawal, condition or silence wording anywhere
voids it, and unusual phrasing fails closed. It also needs a current owner card, a matching action in
the current turn, and no later source turn. Markerless and routine completion,
closure, status, result, release, follow-up, wrap-up and internal sources are
excluded; a carrier label is provenance only, never notification authority, and an
unsolicited decision stays silent under the no-chatter policy. Cancelled, superseded,
closed-channel and quiet-hours states are accepted only from transport-authored
source metadata bound to the exact request ID; incidental source words never
bypass the check. A quiet-hours deferral is pending only while its source-bound
owner card and configured destination remain current. A consultation with no
action is excluded.

After an action, the same bounded gate requires one gate-owned durable receipt for
the configured Telegram destination. Create it only with:

`hermes completion-gate receipt-send --source-request-id <id> --card <t_id> --message-file <absolute-path>`

The command derives profile/session/current card from native scope, resolves one
exact persisted Slack source row, verifies the source-turn card action and fixed
configured destination, then calls the same native helper as `hermes send`.
It records the source identity, card, destination/session, message hash, provider
message ID and mirror disposition in the gate database; terminal stdout is never
evidence. Dedupe is bound to profile + source channel + source request ID; mutable
session/timestamp metadata cannot authorize a second transport. A reservation
written before transport makes retries at-most-once.
Transport-only retries invoke the native mirror helper only; ambiguous/unavailable
transport remains reconciliation/no-resend. The before-turn checker preserves those
no-resend dispositions and revalidates the exact source row/channel, same-source-turn
action, current card, configured destination/session and exact destination mirror.
Legacy in-memory/persisted `send_message` evidence remains accepted for migration
coverage, but the unrestricted model tool stays unregistered.

`inaction_enabled` treats FIRST-PERSON inaction about an obligation (“I did not /
could not / can't / won't” + an action verb such as send, fix, finish, restart,
proceed) and the fixed no-third-state phrases as an uncarded
commitment. Third-party or factual negation (“it can't be dissolved”, “the backup
didn't run”, “agents can't restart the Mac”) and first-person perception (“I can't
see/measure”) are status, not inaction, and never fire unless the current turn records a successful corrective lifecycle
action bound to the named obligation, or a `needs_input` escalation. Its repair is:
“Take the action or escalate to
Lars — no third state.” Both classes reuse the existing reentrancy, reason dedup,
single repair bound and escalation ceiling. Metrics add subtype counts without
changing the existing claims/error denominators.

First-person negations inside paired `"…"`, `“…”` or backtick spans are reported
text, not self-reported inaction. Unclosed spans and single quotes do not exempt
anything; unquoted obligations later in the answer are still checked. The fixed
no-third-state phrases are unchanged. A narrow future promise (`won't` / `will
not` + `change|touch|alter|override`) to preserve `your decision` / `your choice`,
or `that|it|this` immediately tied to `as you decided/chose/specified/requested`
or `as you set it/her/him/them`, is also excluded. Bare `I won't change that`,
inability, past inaction, conditional attribution and delayed changes still
fail when they match the existing verb list. The verb list is not expanded
(`touch`, `alter` and `override` were not triggers before this exception).

### Bounded failure diagnostics and recovery

Extraction retries at most once for classified transport/server/timeouts, only
inside the original monotonic `check_timeout_seconds` whole-check deadline.
Each subprocess receives only remaining time. `auxiliary.transient_retries: 0`
disables the outer retry too; positive values are capped at one outer retry.
Native auxiliary retries/fallbacks still run inside each child, not beside the
budget. There is no guarantee of only two HTTP
requests (the native router owns its internal recovery).

The child watchdog includes native routing, queueing, retries and response parsing.
Its transport timeout and watchdog enforce the SAME absolute monotonic deadline;
there is no startup reserve or fractional allocation. Parent or child deadline
expiration reports `aux_timeout`. Route `unknown` means routing
had not been observed, not evidence of provider latency. OS process startup/reap
and SQLite lock latency are not hard-real-time; no new attempt gets a fresh total
budget. The child uses the native served-profile environment factory rather than
inheriting another profile's credentials.

Every failed attempt logs a sanitized diagnostic dictionary, including recovered
first attempts. Terminal `gate_error` rows add a `diagnostics` JSON column;
existing rows retain `{}`, and old plugin versions can still read the database.
Fields are closed exception/cause/provider labels, bounded elapsed seconds,
attempt (1/2), child exit code, and `aux_model_sha256`. The model is a fingerprint
of the native router's **actually selected** `route_info.model`, not a guessed
configured route. Match it to trusted configured/catalogue model names using
SHA-256; raw child model strings are deliberately never copied into logs/SQLite.
Unknown exception/provider names become `unknown`; arbitrary stderr, stdout,
exception messages, answers and reference values never become diagnostics.
Child-native logging is disabled because SDK/router error logs can contain these
values. No credentials, URLs or headers are included in diagnostics.

A provider `content_filter`/`refusal` finish with null content is classified
`content_filtered`, not an opaque JSON `TypeError`. It is **not transient**: no retry or filter-bypassing
fallback is added. Such responses still produce the existing unverified/error
fail-open verdict. Reliability recovery is not a promise of zero fail-open
outcomes, and it does not change blocking or escalation policy.

Read-only checker children reproduce file existence/nonempty (absolute, `~/` or
`dropbox:`-relative paths, resolved against the Dropbox client's personal root;
directories must be nonempty; never cwd-relative), sha256 (full or >=8-hex prefix,
files <=256 MB), Kanban card existence plus optional status/assignee across the
native boards, compared jointly with the card's present row (the only history
consulted is a dispatcher claim that opened the current run in the last 15
minutes: running then also matches its source status, review or ready, with the
unchanged assignee; superseded transitions and former holders never match), HTTP HEAD
status (no redirects/credentials/query; 401/403/429 against another expected
status is `unverifiable_from_client`, not failed), TCP connection, process
existence, JSON/YAML config equality, exact SQLite/kanban row presence, native
terminal-result exit code, and cron ID presence. The extractor never supplies
executable commands and omits non-claims (plans, in-progress work, negatives and
no-ops, blockers, analysis, statistics, explanations). `turn_receipt`,
`agent_report`, `external_state` and `qualitative` are accepted, distinct kinds
recorded as unverified with mismatch `unverifiable_by_design`; `unknown_kind` is
reserved for genuine completion claims fitting no kind.
References not explicitly supplied in the answer are unverified, not reproduced. This is not
a truth guarantee: extraction can omit/misclassify claims. Live extraction quality
and unverified rate must be accepted by the owner.

Defaults under `plugins.entries.completion-gate.settings`:

| Setting | Default |
|---|---|
| enabled | false |
| max_blocks | 2 (per block window) |
| block_window_seconds | 14400 (4 h) |
| check_timeout_seconds | 60 seconds for the entire check |
| max_claims | 20 |
| max_answer_chars | 32000 |
| db_path | default Hermes root / state/completion-gate/gate.db |
| chat_receipts_enabled | false |
| inaction_enabled | false |
| chat_source_channel | C0BTEFMAAJX |
| telegram_destination | empty (required before receipt enablement) |
| telegram_session_id | empty (required before receipt enablement) |
| state_db_path | empty (required before receipt enablement) |
| kanban_db_path | empty (required before receipt enablement) |

The estate default resolves to `/Users/claudia/hermes/home/state/completion-gate/gate.db`.
Hermes should use `plugins.hook_callback_timeout: 65` at release: five seconds
of framework grace for process reaping and sanitized audit persistence, NOT
additional model or verification time. The callback clock starts on entry,
before config/identity lookup; extraction, transport, verification, event scans
and work-phase SQLite waits share that deadline. SQL execution is interrupted
at expiry; SQLite busy waits are refreshed from remaining time per statement.
Diagnostic persistence retains the existing one-second SQLite busy timeout.
If storage is unavailable, delivery remains fail-open and logs audit unavailable.
No implementation can guarantee persistence on unavailable storage or timely
return from an uninterruptible OS filesystem/process operation.
Legacy extract_timeout, total_timeout and check_timeout are not read by the
adapter; check_timeout_seconds is the only clock setting (contract-tested).

A block is reserved atomically in append-only SQLite. Counters are scoped by
profile and `HERMES_KANBAN_TASK`, falling back to session ID (then task ID), so
restart cannot reset the task ceiling. The ceiling, repeat-reason check and
escalation dedup count only rows inside `block_window_seconds`: a long-lived
session (a DM open for weeks) regains its budget instead of failing open forever
after its first two blocks. Same normalized claim/mismatch with exact reference
cannot block twice within the window. Only one block is honored per turn; its rework iteration is
passed through, not independently certified. A later turn can reproduce the fix.
Concurrent callbacks cannot exceed the ceiling. Recursive calls bypass extraction.

SQLite stores claim hashes, whitelisted artefact kinds, deterministic mismatch
codes, verdicts, reason hashes, block counts and routing/advice markers. Receipt
rows additionally store the non-content source identity, configured destination,
native provider message ID and owner-card ID. Raw
answers, references, config values and exception messages are not stored; the
hash permits correlation without persisting potentially secret claim text.
Mismatch details exist only in the transient model repair request. Both rejected
answer and repair request are excluded from durable conversation history.

Unverified rate uses logged claim records as denominator; an extraction/crash
failure contributes one unverified sentinel, not an invented claim count.
Rework bypasses have no claim records and are separately visible as
`reentrant_pass`. Metrics are evidence coverage, not percentage factual accuracy.

## Escalation and explicit advice markers

Worker ceiling/repeated reason => #hermes-bridge (C0BTEFMAAJX, generalist);
generalist => #gurney-bridge (C0BTH5ZQH61, gurney). Delivery is the native
`hermes send -t slack:<channel>` path (`send_message_tool`) run as the generalist
profile, whose gateway owns the estate Slack bot. (The earlier live-owner inbound
lane returned `escalation_unavailable` for every escalation ever raised.)

At-most-once: a receipt is reserved durably in the TARGET profile home
(`runtime/completion_gate_escalations/<sha256(channel, message)>.json`) before the
POST and resolved to `sent` (with the Slack message ts), `unavailable` or
`unverified`. `unavailable` requires positive proof nothing was posted: the native
send refused before the platform dispatcher was entered (target, config, token,
relay authorization), or Slack's own parsed answer to every chat.postMessage attempt
(observed at the native sender's `_slack_json_post` seam) is an explicit `ok: false`
carrying a definite-refusal code (`SLACK_DEFINITE_REFUSALS`, e.g. `channel_not_found`).
Every attempt is entered in that census before it is awaited, so with multi-workspace
token fallback an attempt that raises (e.g. token A refused, token B's response lost)
stays in the census as incomplete and makes the whole send `unverified`.
The native `Slack API error: ...` text alone is not proof: it is emitted for a malformed
`{}` body too. Any other failure after dispatch (lost response, dropped connection,
timeout, malformed/missing `ok`, `ok: false` without a code or with a may-have-posted code
such as `internal_error`, unknown result) is `unverified`, because Slack may have
accepted the POST. An identical escalation returns
the stored receipt and is never re-posted; a reservation left unresolved by a
timeout/crash reads back `unverified`, not a retry. Within the block window a
reason is deduplicated unless its prior attempt is provably undelivered
(`escalation_unavailable`). Lars is never a target.

Reservation and send execute in a dedicated child under the SAME absolute callback
deadline. The parent kills and reaps on expiry; on POSIX a child alarm also exits at
that absolute deadline, including while blocked on the receipt lock. No detached
callback worker can post a stale message after return. The child runs with the
generalist profile's own scoped credentials only (`served_profile_child_env`).

Before escalation I/O, an append-only `escalation_pending` row reserves the reason
and stable event_id without counting a completed evaluation or duplicating claim
coverage. Success/non-timeout failure then appends the ordinary `fail_open` plus
receipt status; expiry instead appends exactly one `gate_error/aux_timeout`.
Pending reservations retain existing dedup behavior even if a child dies; they
never grant another block/exit. No SQL lock is held across escalation I/O.
An admission committed before expiry is not revoked if a later receipt/audit
step times out. This preserves native at-most-once semantics; it is not an
atomic transaction spanning the mailbox and gate DB.

Gate-authored inbound messages are exempt only when they match a durable event
and its recipient exactly. Arbitrary magic prefixes do not disable the gate.

The CLI is registered through `ctx.register_cli_command`:

    hermes completion-gate metrics
    hermes completion-gate metrics --since 1789800000 --until 1789886400
    hermes completion-gate advice --profile generalist --task TASK_ID --marker advised
    hermes completion-gate advice --profile generalist --task TASK_ID --marker tried

Gurney/Hermes explicitly record advice then its attempted application. A further
false result emits `advice_failed_again` to Gurney. Only Gurney's owner-managed
monitoring may escalate that case to Lars; this plugin never sends to Lars.
Normal reporting is aggregate counts, per-profile counts and unverified rate.
`evaluated_turns` counts deliver, block, fail_open, reentrant_pass and gate_error;
`gate_error_rate` uses that denominator. `action_counts` reports every audit action,
including advice and escalation bookkeeping excluded from the denominator.
Reentrant passes are counted gate decisions, not independently verified answers.
Metrics use the half-open event-created interval [since, until). Bounds accept
unix seconds or ISO-8601 (naive timestamps use local time; explicit offsets are
supported). JSON echoes resolved bounds in `window`; null bounds mean unbounded.
No flags means lifetime, not post-release acceptance. Claim coverage and all
per-profile aggregates use the same selected window.
The separate nightly recommendation cron is Hermes's deployment responsibility.

## Deployment (Hermes only, after Gurney acceptance)

1. Follow `UPDATE-PROCEDURE.md` for the separate external package and generic
   core contribution. Preserve the prior release SHA. Do not cherry-pick the
   distribution branch into upstream or point live profiles at a scratch worktree.
2. For each owner-approved profile, run the following with that profile's
   HERMES_HOME (preserves existing plugin enablement):

       HERMES_HOME=/absolute/profile/home hermes plugins enable completion-gate --no-allow-tool-override
       HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.db_path /Users/claudia/hermes/home/state/completion-gate/gate.db
       HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.max_blocks 2
       HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.enabled true

   Existing configured profiles retain their entire entries unchanged. For a new
   profile, link the shared external package before opting in. Future profiles do
   not inherit root discovery. Do not replace `plugins.enabled` with a singleton.
3. Verify effective auxiliary routing (`auxiliary.completion_gate`, or native
   auto route), shared DB permissions, and that `hermes -p generalist send --list slack`
   resolves both bridge channels. No provider credentials are added by this plugin.
4. Restart/reopen affected CLI/gateway runtimes through the native lifecycle
   owner so the new code and plugin registrations load. Read back discovery and
   effective profile config. No restart is performed by this candidate.
5. Live acceptance: ask a real turn to claim an absolute nonexistent file exists.
   Read the block event and mismatch, observe same-turn repair/corrected answer,
   and confirm rejected text was not displayed or persisted. Test a truthful
   absolute nonempty file claim on a fresh turn and confirm `reproduced` delivery.
   Exercise ceiling on one stable task with distinct missing paths over turns;
   read escalation event AND the receipt file AND the posted bridge message.
   A localhost fake-provider test is not this live acceptance.
6. Owner installs Gurney's separate nightly recommendation-only cron with metrics
   and advice-cycle monitoring. Nothing here creates cron or Telegram sends.

## Kill / rollback

    HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.completion-gate.settings.enabled false

Record the owner's bounded disable authorization with the independent checker
as described in `UPDATE-PROCEDURE.md`; otherwise disabling raises an alarm.
This hot switch stops checking, blocking and routing on the next callback without
restart. A callback already in flight retains its original settings and bounded
deadline. Registration still defers assistant streaming; to restore incremental
text, run `hermes plugins disable completion-gate` for the profile and restart its
runtime. For code rollback, use the owner's native release rollback to the saved
SHA. Preserve gate.db: deleting it would erase persistent safety counters.

## Verification

Run from the candidate/shared repository with a qualified Python environment:

    HERMES_PYTHON=/path/to/python scripts/run_tests.sh tests/plugins/test_completion_gate.py tests/plugins/test_completion_gate_chat_receipts.py tests/plugins/test_completion_gate_checks.py tests/plugins/test_completion_gate_integration.py tests/plugins/test_completion_gate_reliability.py tests/plugins/test_completion_gate_health.py tests/plugins/test_completion_gate_portability.py tests/plugins/test_completion_gate_receipt_send.py tests/agent/test_turn_end_hook.py tests/agent/test_completion_gate_loop.py tests/gateway/test_turn_source_identity.py tests/gateway/test_completion_gate_source_state_clones.py

Tests use real temporary files, SQLite, local HTTP/socket servers, native plugin
discovery, native router against a local provider, the real `hermes send` stack to an
intercepted Slack POST with a durable receipt,
and the actual AIAgent loop/SessionDB with synthetic model responses. They do not
send estate messages, invoke real model providers, or certify live deployment.
