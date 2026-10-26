// Backend subcommand routing for the desktop-managed Hermes process.
//
// The desktop app launches its own headless backend via `hermes serve` — it
// must NEVER depend on or launch the browser `dashboard`. But `serve` is a
// newer subcommand: a runtime that predates it (an older managed install the
// app hasn't updated yet, or an older `hermes` resolved from PATH) only knows
// `dashboard --no-open`. To avoid bricking those users mid-upgrade we detect
// whether the resolved runtime understands `serve` and, only when it does not,
// fall back to the legacy `dashboard --no-open` invocation. Both produce the
// exact same headless gateway; `serve` is just the decoupled name.
//
// These helpers are pure so they can be unit-tested without Electron.

/**
 * Build the canonical headless backend argv (always `serve`).
 * @param {string} [profile] optional Hermes profile to pin via `--profile`.
 */
export function serveBackendArgs(profile?: string) {
  const head = profile ? ['--profile', profile] : []

  return [...head, 'serve', '--host', '127.0.0.1', '--port', '0']
}

/**
 * Interpreter args for a module-form Hermes backend: `-P -m hermes_cli.main <backendArgs>`.
 *
 * The backend is spawned with the user's project as cwd (the app's `hermesCwd`, or the
 * open-in-terminal script's `cd <project>`). Bare `-m` puts that cwd first on `sys.path`,
 * so a project that is itself a hermes-agent checkout would supply `hermes_cli` ahead of
 * the selected runtime root on PYTHONPATH. `-P` (Python >= 3.11, the supported floor)
 * drops only that entry; the child's own tool subprocesses keep normal cwd imports.
 */
export function hermesCliModuleArgs(backendArgs: string[] = []) {
  return ['-P', '-m', 'hermes_cli.main', ...backendArgs]
}

/**
 * Rewrite a resolved backend argv from `serve` to the legacy
 * `dashboard --no-open` form, preserving every other argument (incl. a leading
 * `-m hermes_cli.main` and any `--profile <name>`). Returns a copy; if there is
 * no `serve` token the argv is returned unchanged.
 */
export function dashboardFallbackArgs(args) {
  const i = args.indexOf('serve')

  if (i === -1) {
    return args.slice()
  }

  return [...args.slice(0, i), 'dashboard', '--no-open', ...args.slice(i + 1)]
}

/**
 * True when a runtime's `hermes_cli/subcommands/dashboard.py` source registers
 * the `serve` subcommand. Matches `add_parser("serve"` / `add_parser('serve'`
 * specifically so the substring "server" (e.g. "start_server", "web server")
 * never produces a false positive.
 */
export function sourceDeclaresServe(dashboardPySource) {
  return /add_parser\(\s*["']serve["']/.test(String(dashboardPySource || ''))
}
