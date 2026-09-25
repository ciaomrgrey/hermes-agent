# Domain code policy

Default-off external Hermes plugin that prevents configured domain profiles from authoring substantive executable task tooling before native tool dispatch. It uses the upstream `pre_tool_call` hook; it does not modify Hermes core and is independent of `completion-gate`.

## Scope

When `enabled: true`, the default profile allowlist is `emma`, `sophia`, `plutus`, and `jared`. Cody and every unlisted profile are unaffected.

The policy inspects code-like payloads carried by:

- `write_file`
- replace and V4A multi-file `patch`
- `execute_code`, including statically resolvable nested `write_file`, `patch`, `terminal`, and `tool_call` invocations
- common non-adversarial terminal authoring forms: heredoc redirection, `echo`/`printf` redirection, and Python `-c`

Python source is parsed with `ast`; other supported source extensions use bounded lexical classification. State-mutating filesystem, network, subprocess, SQL, and persistence operations are substantive. Control flow combined with data/API/persistence/error-handling logic is substantive. Syntax or carrier shapes that are recognizably code-like but cannot be classified fail closed. Non-code writes, prose/data work, native domain APIs, ordinary CLI usage, and trivial one-line probes pass.

A block happens before dispatch and returns:

    BLOCKED: substantive executable task tooling is Cody-owned. Reuse or create a Cody Kanban card for <target>.

Decisions are independent per invocation; there is no stateful override or permanent bypass.

## Settings

Settings live under `plugins.entries.domain-code-policy.settings`:

- `enabled`: default `false`
- `profiles`: default `[emma, sophia, plutus, jared]`
- `audit_path`: default `<effective HERMES_HOME>/state/domain-code-policy/audit.jsonl`
- `exceptions`: default `[]`

Audit JSONL stores decision metadata and a SHA-256 content digest, never source content. Concurrent appends are bounded and serialized. Unresolved classifications remain `unresolved` in audit. If an exception cannot be audited, it fails closed.

Each exception is exact and temporary. Every field is required:

    profile: emma
    task_id: t_example
    session_id: session-id
    path: /absolute/canonical/path/to/approved.py
    sha256: <64 lowercase or uppercase hexadecimal characters>
    expires_at: <finite Unix timestamp in the future>

Relative and symlink paths are canonicalized before comparison. A profile, task, session, target, content, or expiry mismatch denies the exception.

## Candidate installation and activation

This task produces a candidate only. It does not install, enable, restart, or activate the plugin.

After independent acceptance, Hermes may copy or link this directory as `domain-code-policy` beneath each approved profile's plugin directory, then opt in without replacing existing plugin enablement:

    HERMES_HOME=/absolute/profile/home hermes plugins enable domain-code-policy --no-allow-tool-override
    HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.domain-code-policy.settings.enabled true

Profile runtimes must be restarted or reopened by their lifecycle owner before loaded-runtime acceptance. Read back plugin discovery and effective settings, then exercise one blocked code write and one allowed prose write through the real runtime.

## Rollback

The immediate hot switch is:

    HERMES_HOME=/absolute/profile/home hermes config set plugins.entries.domain-code-policy.settings.enabled false

This disables policy behavior on the next callback. To unload registration entirely, run `hermes plugins disable domain-code-policy` for that profile and restart its runtime. Preserve the audit log. Roll code back by restoring the previously accepted external package revision.

## Verification

From the repository with a pytest-capable Hermes interpreter:

    HERMES_PYTHON=/path/to/python scripts/run_tests.sh tests/plugins/test_domain_code_policy.py tests/plugins/test_domain_code_policy_integration.py

The integration suite uses native plugin discovery and real pre-dispatch tool execution against temporary files and SQLite. It makes no provider calls and changes no live profile configuration.
