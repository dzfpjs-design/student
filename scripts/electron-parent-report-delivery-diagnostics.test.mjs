import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { captureOwnedFailure, runtimeDiagnostic } from './electron-parent-report-delivery-diagnostics.mjs'

test('startup failure captures owned process without returned app and never exports runtime token', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'hsc-diagnostic-test-'))
  try {
    const entry = { child: { pid: 123, exitCode: null }, phase: 'runtime', cdpPort: 1234,
      runtime: { status: 'starting', error: 'backend failed', sessionToken: 'synthetic-secret', localApiUrl: 'not-exported' },
      logs: () => 'Traceback: synthetic-secret password=another-secret',
      cdp: { events: [{ method: 'Runtime.exceptionThrown', params: { secret: 'synthetic-secret' } }],
        evaluate: async () => { throw new Error('closed') } } }
    await captureOwnedFailure({ outputDir: dir, temporaryRoot: '/synthetic/retained', apps: [entry],
      error: new Error('timeout'), secrets: ['synthetic-secret'] })
    const state = JSON.parse(await readFile(join(dir, 'failure-diagnostics.json'), 'utf8'))
    assert.equal(state.owned[0].phase, 'runtime')
    assert.equal(state.retained_fixture, '/synthetic/retained')
    assert.equal(state.smtp_messages, 0)
    assert.deepEqual(state.capture_errors, [{ artifact: 'owned-1-ui.txt', code: 'CDP_UNAVAILABLE' }])
    assert.deepEqual(state.owned[0].runtime, { status: 'starting', error: 'backend failed' })
    const log = await readFile(join(dir, 'owned-1-electron.log'), 'utf8')
    assert.match(log, /Traceback/)
    assert.doesNotMatch(log, /synthetic-secret|another-secret/)
    assert.equal((await stat(join(dir, 'failure-diagnostics.json'))).mode & 0o777, 0o600)
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('no renderer and artifact write failure do not replace original business failure', async () => {
  const failures = await captureOwnedFailure({ outputDir: '/nonexistent/hsc-test-artifact',
    temporaryRoot: '/synthetic/retained', apps: [{ child: {}, phase: 'spawn', logs: () => 'spawn failed' }],
    error: new Error('original startup failure') })
  assert.equal(failures.length, 2)
  assert.equal(runtimeDiagnostic(undefined), null)
})

test('launcher collects ownership before cleanup and retains failed fixture', async () => {
  const source = await readFile(new URL('./electron-parent-report-delivery-e2e.mjs', import.meta.url), 'utf8')
  assert.match(source, /apps: \[\.\.\.owned\.values\(\)\]/)
  assert.match(source, /if \(!failure && !cleanupErrors\.length\)/)
  assert.ok(source.indexOf('await captureOwnedFailure') < source.indexOf('const cleanupErrors = []'))
})
