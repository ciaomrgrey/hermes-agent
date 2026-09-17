# CHAT-USAGE-GUARD-001 — C2 correction handoff

Task t_ef90f96a. Builder verification only; request same-card independent review by Gurney. NOT deployed, activated, armed, resumed, or delivered to Telegram. Hermes retains release disposition, deployment, real Telegram receipt, and provider-isolation/liveness acceptance. Original 16 September 11:20 CEST start and MISSED 14:20 CEST commitment stand unchanged.

## Exact source and scope

- Existing worktree: /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/candidate
- Existing branch: cody/usage-guard-t_8dbb6c79
- Original base: 6cc8c0c5f9d7a2f08971de585e584e0c901d2c28
- Resumed source: 863c6185cc6c7b9582c637780a7c187894b7bafe, with documentation-only HEAD facc2a441e8e12d6f438b13b613cf4bbb637a02a. Verified no tracked source differences or staged edits on arrival. Historical untracked files preserved.
- Corrected source: 2417ffcdc5526f9005df837c219256edfce98ac3. Only four files changed: agent/provider_control.py, agent/usage_guard.py, tests/agent/test_provider_profile_layout.py, tests/agent/test_usage_guard_reset_identity.py.
- This handoff is a subsequent documentation-only commit. No DNS/resolver, transport, activation, model/config, cron, auth, credential, or delivery changes.

## Required change 1: profile policy visibility

Root cause: current_policy temporarily forced get_default_hermes_root(), discarding the process's actual profile home. Native `hermes -p <profile> config set` correctly wrote profile config, but the policy read root config instead.

Fix: load_config_readonly() now reads the process's effective native home directly. No new configuration inheritance, fallback reader, or path override. Each controlled profile must explicitly configure the SAME canonical database, as the existing installation recipe requires. The setting writer and runtime reader now share the profile config file.

New test executes native `-p generalist config set` and `-p cody config set` in disposable `<root>/profiles/<name>` homes, each pointing to one real SQLite holds database. Fresh child processes read actual policy, reject held Codex, and allow Anthropic. Root config is not created. This is a native configuration/SQLite subprocess test, not a live estate install or a new provider-by-host Cartesian-product claim.

## Required change 2: weekly identity jitter

Root cause: exact comparison of floored reset seconds split a six-hour +11pp series into partial +5.5pp segments when 1789772400.24 became 1789772399.9 (and vice versa). Incident epochs also used exact reset equality.

Fix: same_week binds equal grant/duration/field within inclusive 60 seconds of an observed reset anchor. rolling compares against the segment's fixed first anchor, not pairwise drift. sample binds reset to the latest persisted valid anchor inside the existing transaction, preserving raw_reset unchanged; restart and incident dedupe reuse the bound reset. No time-bucket rounding, schema migration, or history rewriting. Existing gaps/corrections still break coverage.

New tests cover both live-shaped float directions, full sampled +11pp, real persisted samples, unchanged outbox original after restart and reverse jitter, raw_reset retention, a genuine +604800-second new week with no bridged delta and a new incident, mismatched grant/field/duration, inclusive 60-second binding, rejection at 61 seconds, and cumulative pairwise-drift rejection.

## Actual-provider classification

HERMES-MODEL-SWITCH-FABLE-001 is respected: generalist conductor is anthropic/claude-fable-5; its delegated children/cron remain openai-codex per owner steering. No fixed generalist=Codex mapping exists in the candidate's policy or measurement code, so no mapping change or extra mapping test was added. ActiveScope/controlled_turn use the actual agent.provider; request checks receive actual request provider; check_profile loads model.provider and checks the explicit provider override as well. Existing original-provider fallback restrictions remain unchanged. Source measurements are keyed by provider and stable grant, not profile name. CONTRACT.md's old profile/provider enumeration is historical, not runtime truth or a current inventory. No live model/config changes or quota probes were performed.

## Executed gates and receipts

All commands run from candidate using scripts/run_tests.sh (sanitized test environment, temporary HERMES_HOME, per-file processes). Receipts are in the existing parent workspace /Users/claudia/hermes/home/profiles/generalist/workspace/usage-guard-20260916/.

1. `scripts/run_tests.sh tests/agent/test_provider_profile_layout.py -j 1`
   - c2-profile-red.txt: exit 1, 1 failed, actual policy database=None and held=False. Tested before production edits against original candidate source.
   - c2-profile-green.txt: exit 0, 1 passed after the reader fix.
2. `scripts/run_tests.sh tests/agent/test_usage_guard_reset_identity.py -j 1`
   - c2-reset-red.txt: exit 1, 3 failed. Both directions return +5.5pp/partial rather than +11pp/sampled; identity metadata mismatch was also accepted. usage_guard.py remained byte-identical to original candidate for RED (only the separate policy fix existed).
   - c2-reset-green.txt: exit 0, 3 passed after reset binding.
3. Final targeted native suite, including the two new files through the existing globs:

    scripts/run_tests.sh tests/run_agent/test_*persist*.py tests/agent/test_*flush*.py tests/agent/test_turn_finalizer*.py tests/test_state_db_write_durability.py tests/agent/test_session_persistence*.py tests/agent/test_provider_*.py tests/agent/test_usage_guard*.py tests/agent/test_auxiliary_owned_cancel.py tests/agent/test_auxiliary_stream_lifetime.py tests/agent/test_title_generator.py tests/cli/test_single_query_session_finalize.py tests/cli/test_single_query_clarify.py -j 3

   c2-final-qualified-gate.txt: exit 0, 262 passed, 0 failed, 45 files, 236.7 seconds, no flaky-file warning. This is the original 258-test targeted gate plus four new cases, not the repository's full suite. Native disposable install and native host ingress tests remain green. Only the existing disposable install gate installs packages; no production interpreter was modified. Test traffic is local/synthetic, not provider quota or Telegram.

4. git diff --cached --check passed before the source commit; unchanged config.json and deploy/cron-create.json verified with git diff --exit-code HEAD. config.json still armed=false and delivery_enabled=false. Manual added-line review found parameterized SQL, no secrets, no shell=True/eval/deserialization, and no new external network calls.

A pre-existing zero-byte worktree index.lock from 16 September 16:38:59 had no lsof owner; it was preserved as ../index.lock.stale-t_ef90f96a before staging. No running Git process was terminated. Existing untracked artifacts/state remain excluded from commits and deployment.

## Rollback and remaining release boundaries

No live rollback is needed: nothing deployed. Local correction rollback, if requested, is a revert of 2417ffcdc5526f9005df837c219256edfce98ac3 on this branch; it needs no database migration but intentionally restores both reviewed defects. Do not roll back enforcement in a live estate with active holds without Hermes's release decision.

All earlier residuals remain: unbounded DNS, finite TCP drain not remote billing proof, lossless pending alerts imply unbounded outage storage, external Chat/Claude.ai usage remains uncontrolled, CLI arming refusal remains untouched. Gurney's independent correction review is next; Hermes still must validate effective live configuration/account inventory, actual provider isolation, no_agent liveness, and real Telegram receipt after explicit release disposition. Build PASS does not close the live user goal.
