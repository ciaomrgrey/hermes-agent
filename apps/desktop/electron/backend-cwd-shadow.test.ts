import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { buildDesktopBackendEnv } from './backend-env'
import { hermesCliModuleArgs } from './backend-command'
import { buildTerminalScript, terminalScriptEnv } from './external-terminal'

// Behavior-level regression for the project-cwd import shadow (t_bbff8aaf, Gurney round 2):
// open-in-terminal writes `cd <project>` then execs the module-form backend; the app's own
// backend spawn uses the same descriptor with the project as cwd. When that project is a
// hermes-agent checkout, bare `-m` imported ITS hermes_cli ahead of the runtime root on
// PYTHONPATH. Hermetic: a fake runtime root and a decoy project, real /bin/sh + python3.

function python311(): string | null {
  for (const candidate of [process.env.HERMES_TEST_PYTHON, 'python3']) {
    if (!candidate) continue
    const r = spawnSync(candidate, ['-c', 'import sys; print(sys.version_info >= (3, 11))'], { encoding: 'utf8' })
    if (r.status === 0 && r.stdout.trim() === 'True') return candidate
  }
  return null
}

function fixture() {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'cwd-shadow-'))
  const runtime = path.join(base, 'runtime')
  const project = path.join(base, 'project-is-a-checkout')
  const marker = path.join(base, 'decoy-imported')
  fs.mkdirSync(path.join(runtime, 'hermes_cli'), { recursive: true })
  fs.writeFileSync(path.join(runtime, 'hermes_cli', '__init__.py'), '')
  fs.writeFileSync(path.join(runtime, 'hermes_cli', 'main.py'), 'import sys\nprint("RUNTIME", *sys.argv[1:])\n')
  fs.mkdirSync(path.join(project, 'hermes_cli'), { recursive: true })
  const hit = `import pathlib, sys\npathlib.Path(${JSON.stringify(marker)}).write_text(__file__)\nsys.exit(97)\n`
  fs.writeFileSync(path.join(project, 'hermes_cli', '__init__.py'), hit)
  fs.writeFileSync(path.join(project, 'hermes_cli', 'main.py'), hit)
  const home = path.join(base, 'hermes-home')
  fs.mkdirSync(home)
  return { base, runtime, project, marker, home }
}

function runTerminalScript(python: string, args: string[], fx: ReturnType<typeof fixture>) {
  const env = buildDesktopBackendEnv({ hermesHome: fx.home, pythonPathEntries: [fx.runtime], currentEnv: {} })
  const script = buildTerminalScript({
    command: python,
    args,
    cwd: fx.project,
    env: terminalScriptEnv(env, fx.home),
    platform: 'darwin'
  })
  return spawnSync('/bin/sh', ['-c', script], { encoding: 'utf8', env: { PATH: process.env.PATH ?? '/usr/bin:/bin' } })
}

const py = process.platform === 'win32' ? null : python311()

test.skipIf(!py)('open-in-terminal module backend in a checkout project runs the selected runtime', () => {
  const fx = fixture()
  try {
    const r = runTerminalScript(py!, hermesCliModuleArgs(['--tui', '--resume', 'sess']), fx)
    assert.equal(fs.existsSync(fx.marker), false, `decoy imported: ${fs.existsSync(fx.marker) && fs.readFileSync(fx.marker, 'utf8')}`)
    assert.equal(r.status, 0, r.stderr)
    assert.equal(r.stdout.trim(), 'RUNTIME --tui --resume sess')
  } finally {
    fs.rmSync(fx.base, { recursive: true, force: true })
  }
})

test.skipIf(!py)('control: the bare -m form really is shadowed by the checkout project', () => {
  const fx = fixture()
  try {
    const r = runTerminalScript(py!, ['-m', 'hermes_cli.main', '--tui'], fx)
    assert.equal(r.status, 97)
    assert.equal(fs.existsSync(fx.marker), true)
  } finally {
    fs.rmSync(fx.base, { recursive: true, force: true })
  }
})

test('hermesCliModuleArgs keeps backend args after the pinned module launcher', () => {
  assert.deepEqual(hermesCliModuleArgs(['serve', '--port', '0']), ['-P', '-m', 'hermes_cli.main', 'serve', '--port', '0'])
})
