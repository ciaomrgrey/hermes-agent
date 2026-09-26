# External distribution and update contract

## Ownership and deliverables

Gurney reviews; Hermes activates, restarts and verifies the serving fleet. This
candidate does not deploy itself, mutate profile settings or install a scheduler.
The distribution branch is an assembly/review artifact, NOT an upstream fork to
maintain. Its `external/completion-gate/` directory is the standalone package.
Install that directory into the user plugin tree, outside `hermes-agent/`.

Canonical estate destination:

    /Users/claudia/hermes/home/plugins/completion-gate/

Copy the sibling `completion-gate-health.py` separately to:

    /Users/claudia/hermes/home/checks/completion-gate-health.py

The check must NOT live inside the package whose disappearance it detects. It
uses only the installed native runtime plus stdlib, not a completion-gate import.
Keep its source with the external distribution. No core file contains a
completion-gate discovery special case. Upstream updates must never be delayed,
pinned, or made responsible for retaining this estate package.

Native discovery scans `$HERMES_HOME/plugins/`, not the default root as fallback.
For each already-enabled named profile, link its own user-plugin path to the
canonical package (after inspecting any existing destination; do not overwrite a
conflicting package):

    mkdir -p "$PROFILE_HOME/plugins"
    ln -s /Users/claudia/hermes/home/plugins/completion-gate "$PROFILE_HOME/plugins/completion-gate"

Use a real copied package on platforms where symlinks are unavailable. The
A→B→A shared-link test verifies settings remain profile-local. Do not link config,
credentials, state or whole homes. Keep every existing
`plugins.entries.completion-gate` value and `plugins.enabled` unchanged. The
existing DB remains `/Users/claudia/hermes/home/state/completion-gate/gate.db`.
Remove the obsolete tracked bundled package only as part of Hermes's reviewed
release integration; do not leave divergent copies for a future updater.

## Generic core hook until upstream merges it

Local contribution branch: `cody/before-turn-end-upstream`.
Initial base: `27dfbe397f8`.
Core-only commit: `436e2419278` (full SHA in the task handoff).
Portable patch: `before-turn-end.patch` in the release artifact.

This contains only the generic additive hook, turn identity/output-transform
threading, streaming/persistence protection, documented contract and invariant
consumer tests. It contains NO estate package, checker, config, database or
routing policy. It is prepared for contribution to NousResearch/hermes-agent;
no PR has been published. The concrete consumer is the separately distributed
completion evidence plugin. The core is still update-sensitive UNTIL upstream
accepts it; external placement alone does not change that fact.

Exact operator re-apply procedure after an upstream update (a maintenance window;
the checker must remain running and flag missing-hook incidents):

    git -C /Users/claudia/hermes/home/hermes-agent status --short
    git -C /Users/claudia/hermes/home/hermes-agent rev-parse HEAD
    git -C /Users/claudia/hermes/home/hermes-agent worktree add -b completion-hook-reapply /absolute/new/reapply-worktree HEAD
    git -C /absolute/new/reapply-worktree am /absolute/durable/release/before-turn-end.patch
    cd /absolute/new/reapply-worktree
    HERMES_PYTHON=/Users/claudia/hermes/home/hermes-agent/venv/bin/python scripts/run_tests.sh tests/agent/test_turn_end_hook.py tests/agent/test_turn_end_hook_loop.py --file-retries 0

Do not apply again if the generic hook is already present. A conflict is a real
porting requirement: stop the apply, inspect the current native code and restore
the invariants; never force old whole files over refactored upstream. Abort an
unwanted application with `git am --abort`. To rebase the contribution branch
itself, use `git fetch origin main` then `git rebase --onto origin/main
27dfbe397f8 cody/before-turn-end-upstream`, resolve semantically, rerun the same
core tests plus native finalizer/stream/persistence gates, and regenerate the
patch with `git format-patch -1 --stdout`. Do not call a rebase verified before
executing the gates on its exact HEAD.

Hermes then integrates the verified core through the native release process,
restarts the affected runtimes, reads back loaded code/discovery and runs the
post-update checker. Do not maintain an updater wrapper or auto-patch core from
the plugin. Once the hook lands upstream, delete this temporary patch/reapply
obligation; keep the external plugin.

## Post-update and scheduled health invocation

Run independently of the plugin's enabled state, using the installed interpreter:

    /Users/claudia/hermes/home/hermes-agent/venv/bin/python /Users/claudia/hermes/home/checks/completion-gate-health.py --home "$PROFILE_HOME" --state /Users/claudia/hermes/home/state/completion-gate/health.db

Run after EVERY update, including an update that returned nonzero; do not connect
it with success-only `&&`. Also schedule it using native Hermes cron
`script` + `no_agent=true` for each explicitly protected profile (suggested cadence
one minute). The script is the job, not an LLM prompt. No native scheduler is
installed by this candidate. Hermes must exercise it once via the actual cron
runner, verify the native execution receipt and Gurney's inbound receipt, and
record the job ID during activation. Do not rely on the watched plugin's own hooks
or plugin-registered CLI command to launch this check: both vanish when disabled.

Exit 0 = checked without an alarm; exit 1 = alarm/check failure. stdout contains
bounded JSON reasons and native delivery status. Reasons include:

- `package_unavailable`: enabled package missing or import/registration failed.
- `core_hook_missing` / `hook_unregistered`: source updated without the generic hook.
- `unauthorized_disable`: allow-list removal, deny-list inclusion, entry disabled,
  or settings.enabled no longer exactly true, without an exact unexpired marker.
- `liveness_gap`: persisted nonempty assistant text without tool calls, older than
  60 seconds, lacks nearby gate activity for the SAME profile. A prior cursor is
  retained across checker restarts. First run looks back ten minutes; subsequent
  checks advance only healthy intervals, in batches of at most 1000 finals. Gate
  evidence must be between total_timeout+60 seconds before the final and two
  seconds after it. Another profile's DB activity cannot mask silence. No turns
  means no idle-clock alarm. Old unresolved missing events remain visible.
- `health_check_failed`: malformed/unreadable config, schema/DB or checker error.

The checker uses native effective config with profile scope and managed overlay.
Gate and session databases are opened read-only; health cursors/markers/alarms
live in a SEPARATE health.db. No answers, references, config values or credentials
are written there or sent. Profile homes and approval references are hashed.

Alarms go directly to Gurney through the same native Bot Chat live-owner queue
used by gate escalation. Exact message receipts are read back. `queued` is NOT
processed/seen. No live owner or send failure is `unavailable`, still exit 1;
subsequent checks retry the same durable incident rather than silently dropping
it. Native delivery IDs deduplicate queued incidents. Recovery rearms new alarms.
The checker never blocks an agent, enables/disables a plugin, patches core,
contacts Lars, or deploys anything.

Limits: native transcript persistence is proof of turn activity, not a transport
receipt. The activity heuristic cannot certify every final or detect partial loss
when nearby same-profile events exist; it detects the requested silent-death gap.
This is operational tamper detection, not protection against a same-user attacker
rewriting the checker, its schedule, or its marker database. Native cron failure
receipts remain the owner's signal if the independent checker itself is missing.
The plugin's existing `gate_error` fail-open events are not changed or relabelled
as liveness silence.

## Authorized maintenance disable

After an owner-authorized disable, record the exact effective disabled configuration
and an approval reference with a short explicit expiry (CLI permits up to 24h):

    /Users/claudia/hermes/home/hermes-agent/venv/bin/python /Users/claudia/hermes/home/checks/completion-gate-health.py --home "$PROFILE_HOME" --state /Users/claudia/hermes/home/state/completion-gate/health.db --authorize-disable OWNER_APPROVAL_REFERENCE --until UNIX_EXPIRY_SECONDS

This command is an explicit operator audit marker, not permission granting and not
a signature proving Lars personally approved it. It never changes config. A new
plugin config digest or expiry invalidates the marker; an unexplained removal of
the allow-list alerts even on the checker's first run. An unrelated profile's
marker cannot suppress another profile's alarm.

Rollback: restore the previous external package/core release using Hermes's native
release procedure; preserve gate.db and health.db. A deliberately disabled gate
needs the bounded marker above. Do not remove monitoring as part of a gate rollback.
