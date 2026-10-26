/**
 * Interpreter args for a module-form Hermes backend: `-P -m hermes_cli.main <backendArgs>`.
 *
 * The backend is spawned with the user's project as cwd (the app's `hermesCwd`, or the
 * open-in-terminal script's `cd <project>`). Bare `-m` puts that cwd first on `sys.path`,
 * so a project that is itself a hermes-agent checkout would supply `hermes_cli` ahead of
 * the selected runtime root on PYTHONPATH. `-P` (Python >= 3.11, the supported floor)
 * drops only that entry; the child's own tool subprocesses keep normal cwd imports.
 * Children with a fixed install-root cwd (e.g. the uninstall summary) may keep bare `-m`.
 */
export function hermesCliModuleArgs(backendArgs: string[] = []): string[] {
  return ['-P', '-m', 'hermes_cli.main', ...backendArgs]
}
