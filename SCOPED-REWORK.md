# Scoped rework, run422 — t_38cfda9d

Owner comments903/906 govern this continuation, superseding broad original implementation acceptance for this pass. Only adjacent-turn liveness and originating-run lifecycle corrections were finalized. This is NOT full Doorman completion. Same worktree/branch: /Users/claudia/hermes/home/kanban/boards/estate/workspaces/t_38cfda9d/candidate, cody/t_38cfda9d. Rework parent: 7b3f3b9a1410180d647fa6697593f5ad73b345ee; original base: 9fa0119cb2b7b1c075037ce8f7fd51a9cde3946c.

## Corrections

external/completion-gate-health.py now assigns each gate event only to its uniquely nearest eligible final within the bounded tolerance. Neighbor lookup includes already-cursored and unsettled finals so a batch boundary cannot lend their event to another final. Ties remain uncovered. One event cannot certify multiple finals. The recorded +17.09s worker persistence case remains green. Timestamp-only evidence still does not prove exact session/turn identity; this is conservative non-reused matching, not a new identity-bearing audit schema.

agent/kanban_stop.py reads the originating task_run and current_run_id read-only. A closed or replaced run receives no mutation nudge, including review_requested and changes_requested. A still-active run retains the nudge, even if its transcript contains an attempted terminal call. An unavailable board or missing run ID requests kanban_show rather than falsely asserting running. Native dispatcher protocol accounting is unchanged. No completion-gate seam or approval policy changed.

Changed tests: tests/agent/test_kanban_stop.py (legacy invocation-only suppression replaced with native-readback requirement), tests/agent/test_kanban_stop_run_ownership.py (real temporary native board transitions), tests/plugins/test_completion_gate_incidents.py (adjacent-turn negative control plus unsettled reservation and positive independent coverage).

## Reproducible verification

Durable submission includes rework.patch, original candidate.patch, changed sources/tests, and red-adjacent.log, red-lifecycle.log, scoped-final-green.log. Original historical packet remains attached to the task as review-evidence.tar.gz; it is historical evidence, not current full-suite verification.

RED adjacent case from run418: 1 failed, 2 passed in red-adjacent.log. A@1000 and B@1070, event@1075 must retain A's gap and not advance cursor. Prior symmetric any-event matching failed.

RED lifecycle rerun in run422: temporarily restored only agent/kanban_stop.py from rework parent with shell trap restoring the preserved green file. Native per-file runner returned1, 0 passed/2 failed: review_requested still nudged and missing-board still asserted running. No second checkout/candidate created.

GREEN final run: 4 files, 15 passed, 0 failed, no retry. Includes changed legacy test, real board review/reclaim/request-changes transitions, attempted-terminal active-run nudge, unavailable board, genuine missing-hook and other-profile isolation, idle/no-turn silence, recorded worker ordering, mirror provenance, confirmed-delivery dedupe/retry/recovery, missing package/core hook and unauthorized disable alarms.

From the candidate directory, reproduce the final scoped suite:

    env -i HOME="$HOME" PATH="$PATH" TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 /Users/claudia/hermes/home/hermes-agent/venv/bin/python scripts/run_tests_parallel.py tests/agent/test_kanban_stop.py tests/agent/test_kanban_stop_run_ownership.py tests/plugins/test_completion_gate_incidents.py tests/plugins/test_completion_gate_health.py --file-retries 0

Runner caveat: the initial scoped command used the repository-mandated scripts/run_tests.sh and passed15/15. Its output revealed a global precompile step. Inspection of that runner then established it invokes compileall over all tracked Python files, potentially including the denied auxiliary source (cached files may be skipped; no per-file trace was retained). This was an inadvertent overly broad runner prerequisite, not an authorized auxiliary access or evidence that the denial is cleared. No denied source content was obtained or used for diagnosis. Do not repeat that runner under this hold. Subsequent RED/GREEN invoked only the existing native per-file runner in the same clean environment, omitting global precompilation. No alternate reader/import/copy was used to obtain auxiliary_client.py contents. The original43-test broader suite was NOT rerun and remains historical. Full loop/auxiliary integration is untested in this revision under the hold.

## Safety and unresolved owner gates

No live/canonical edits, activation, restarts, updater execution, cron edit, provider traffic/refusal retry, external publication or outward notification. Before_turn_end and native gate block semantics remain unchanged; git diff --check passes. agent/kanban_stop.py and the prior gateway/mirror.py core changes require owner's explicitly authorized upstream PR-backed exception or equivalent stock release; no PR was published.

Root t_6397391f retains unresolved timeout/invalid-response incidence diagnosis and repair. The denied auxiliary surface remains held; raw historical response shape/router phase was not retained. Do not substitute synthetic causes for historical facts or invent timeout attribution for unknown rows. Historical window still records479 evaluations/113 gate_errors/23.591%; no new live rate or post-fix day exists. Measured full-day <2% and real release-tag hook survival remain explicit unsatisfied gates. Fixtures cannot satisfy either.

The original HANDOFF.md job proposal is historical: root now records John's native edit/readback of existing cron0d2f45e2570b; Cody made none. Actual cron generation/delivery acceptance remains with root. Existing e9f72f3efbae local-only authority conflict remains owner-held; no new jobs.

Historical unmarked mirror5933 still needs exact owner adjudication; no blanket cursor reset or null-finish exemption. Concurrent/crash-after-send notification windows remain as originally documented.

Review: Gurney same-card bounded technical review. Approval of these accessible fixes must not close root efficacy obligations. Root John/generalist owns activation and operational wake-only handoff; no passive Telegram substitute. Same-card review is intentional despite root being a release child, per explicit latest owner instruction.

Rollback: isolated candidate only, no live rollback needed. Revert the rework commit to return to prior review candidate (which has known adjacent-turn false negatives and must not be activated). Preserve all historical audit evidence.
