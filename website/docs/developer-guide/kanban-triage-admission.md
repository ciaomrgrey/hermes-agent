# Exact-operation triage admissions

Source candidate; default off. This is a narrow native Kanban routing primitive,
not a hold-release API, approval classifier, scheduler or worker spawner.

## Trust and scope

`kanban_admit_triage` is available only to the configured conductor profile in a
non-cron, non-worker, non-delegated session. `kanban_route_triage` is available only
to the distinct configured router profile inside the bound cron job. Runtime
profile scope and dispatcher-injected execution/session IDs identify callers;
model arguments cannot supply identity or approval flags. Handlers repeat these
checks even if the schema was cached or dispatch is called directly.

Operator-owned configuration under `kanban.triage_routing` is absent/null by
default. Its exact keys are `conductor_profile`, `profile` (router), `cron_job_id`
and `board`. Configure the same binding in the two authorized profiles through
native configuration commands only after review/release authorization. Retain the
existing Kanban toolset opt-in. There is no inherited cross-profile config. Copied
configuration does not give an unrelated profile either role. As with other
native tools, this is not an OS sandbox against code that can rewrite config or
SQLite; filesystem/config/dispatcher code are trusted runtime inputs, not model
parameters. Ambient DB overrides and conflicting board pins are rejected.

## Read, admit, consume

1. The conductor uses native `kanban_show` and resolves owner restrictions outside
   this primitive. Event records expose `id`; use the largest as
   `expected_event_id`. This tool does not infer permission from prose or comments.
2. Conductor calls `kanban_admit_triage` with exact `task_id`, `expected_event_id`,
   `action` (`specify`, `promote`, `reassign`), optional `assignee`, and optional
   nonblank `specification` for `specify` only. `reassign` requires an assignee.
   The target must be ordinary never-executed triage/todo intake.
3. Native `triage_admitted` audit storage records version, bound board/profile/job,
   task, exact operation, current task/dependency digest, revision and trusted
   conductor session/execution owner. Minting does not release the task.
4. The scheduled router submits exactly the admitted operation using the returned
   admission event ID. All eligibility checks, latest-event CAS, digest comparison,
   task mutation and `triage_routed` audit append occur in one SQLite write
   transaction. The new event consumes the admission: replay or concurrent reuse
   fails. A new conductor admission also supersedes the prior one. Read the task
   back natively; a successful transition is not a worker claim or execution.

Specification is appended, never a replacement that erases original restrictions.
The exact appended text is admission-bound; a router cannot substitute its own.
No run ID is invented for non-worker calls. Consumer provenance contains actual
runtime profile, cron job, execution and session IDs. Audit-write failure rolls
back both mutation and consumption. Event IDs, not wall-clock order, determine
CAS precedence.

## Fail-closed limits

Absent, unknown, stale, consumed or mismatched admissions deny. Current/prior
blocks, needs_input/capability restrictions, reviewer-derived histories, runs,
comments, unknown lifecycle events and workflow-template records deny even with
conductor access. Reviewed records cannot be assigned back to builders through
this primitive. Unknown origin denies. Any intervening target event invalidates
the grant; full row/dependency hashes also reject changed bodies, owners and
fields lacking a native event. Target profiles must exist on disk.

Original prose-only restrictions (including PARK/account/security/release and
unrecognized wording) remain denied without separate trusted exact admission;
there is deliberately no deny-word dictionary or model-based eligibility grant.
The conductor is responsible for resolving prose restrictions before admitting an
ordinary intake record. This does not authorize clearing an active native hold.
Comments cannot grant admission and currently make the record ineligible.

`specify` requires triage and lands in todo until parents are done, otherwise
ready. `promote` requires todo and completed parents. `reassign` preserves phase.
Readiness does not bypass native dispatcher profile admission, caps, provider
holds or review routing. Existing unblock/specify APIs are unchanged. This new
primitive does not remediate broader trust issues in those pre-existing APIs.

## Activation and rollback boundary

Independent review is required before installation. The release owner must apply
only the reviewed candidate commit/diff against its exact base, run the focused
and regression gates, then separately authorize configuration and runtime
activation. No schema migration is required: existing native task events supply
the append-only admission/consumption ledger. No live grants should be fabricated
for testing. Use temporary boards for acceptance.

For rollback, disable `kanban.triage_routing` in both profiles with native config
commands and verify both schemas/handlers deny, then restore the previous source
under the release owner's normal runtime procedure. Do not delete audit events or
undo landed task state automatically; consumed operations require ordinary owner
reconciliation. Retained unconsumed admissions must not be replayed after
re-enabling without fresh conductor readback/admission. Configuration removal
alone is a disable, not cancellation of already-admitted in-flight transactions.
Drain active routing calls before rollback if that distinction matters.
