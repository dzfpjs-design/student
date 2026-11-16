import test from 'node:test'
import assert from 'node:assert/strict'
import { argumentsFor, buildManifest, creditedSeconds, sameProcess, isolatedEnvironment } from './windows-runtime-stability.mjs'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const paths = ['--electron-bin', 'electron', '--exe', 'backend', '--python-bin', 'python', '--model-dir', 'models', '--output-dir', 'out']
test('soak requires explicit final-build approval and known arguments', () => {
  assert.throws(() => argumentsFor(['--mode', 'soak', ...paths]))
  assert.equal(argumentsFor(['--mode', 'soak', ...paths, '--approved-build', 'a'.repeat(64)])['--mode'], 'soak')
  assert.throws(() => argumentsFor(['--mode', 'gate', ...paths, '--gate-seconds', '28800']))
  assert.throws(() => argumentsFor(['--mode', 'gate', ...paths, '--profile', 'family']))
  assert.throws(() => argumentsFor(['--print-build']))
  assert.equal(argumentsFor(['--print-build', '--exe', 'backend', '--electron-bin', 'electron', '--python-bin', 'python'])['--print-build'], true)
  assert.throws(() => argumentsFor(['--mode', 'recovery', ...paths]))
  assert.equal(argumentsFor(['--mode', 'recovery', ...paths, '--approved-build', 'a'.repeat(64)])['--mode'], 'recovery')
})
test('manifest binds UI assets, runtime inputs and optional package without launching it', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hsc-fingerprint-test-'))
  try {
    const names = ['out/main/index.js', 'out/preload/index.js', 'out/renderer/index.html', 'out/renderer/assets/avatar.png',
      'electron/electron.exe', 'electron/locales/en.pak', 'node.exe', 'python.exe', 'backend.exe', 'app.asar', 'installed.exe',
      'scripts/windows-runtime-stability.mjs', 'scripts/runtime-stability-sqlite.py', 'scripts/electron-harness-env.mjs', 'scripts/electron-owned-process.mjs']
    for (const name of names) { await mkdir(join(root, name, '..'), { recursive: true }); await writeFile(join(root, name), name) }
    const config = { '--exe': join(root, 'backend.exe'), '--electron-bin': join(root, 'electron/electron.exe'), '--python-bin': join(root, 'python.exe'), '--app-asar': join(root, 'app.asar'), '--app-binary': join(root, 'installed.exe') }
    const baseline = await buildManifest(config, root, join(root, 'node.exe'))
    assert.equal(baseline.files.length, names.length + 1)
    for (const name of names) {
      await writeFile(join(root, name), name + ':changed')
      assert.notEqual((await buildManifest(config, root, join(root, 'node.exe'))).digest, baseline.digest, name)
      await writeFile(join(root, name), name)
    }
    assert.equal((await buildManifest(config, root, join(root, 'node.exe'))).digest, baseline.digest)
  } finally { await rm(root, { recursive: true, force: true }) }
})
test('downtime cannot silently grant a completed eight-hour run', () => {
  assert.equal(creditedSeconds(28800), 0)
  assert.equal(creditedSeconds(-10), 0)
  assert.equal(creditedSeconds(60), 60)
})
test('PID alone never authorizes cleanup', () => {
  const expected = { pid: 42, created: 'now', exe: 'C:\\owned.exe' }
  assert(sameProcess({ ...expected, exe: 'c:\\OWNED.exe' }, expected))
  assert(!sameProcess({ ...expected, created: 'later' }, expected))
  assert(!sameProcess({ ...expected, exe: 'C:\\family.exe' }, expected))
  assert(!sameProcess(null, expected))
})
test('isolation restores only explicit local runtime overrides', () => {
  const old = process.env.HSC_DATABASE_PATH
  process.env.HSC_DATABASE_PATH = 'family.db'
  try {
    const env = isolatedEnvironment({ '--python-bin': 'approved-python', '--model-dir': 'approved-models' }, '/synthetic')
    assert(!env.HSC_DATABASE_PATH.includes('family.db'))
    assert.equal(env.HSC_LOCAL_AGENT_PYTHON, 'approved-python')
    assert.equal(env.HTTPS_PROXY, 'http://127.0.0.1:9')
  } finally { if (old === undefined) delete process.env.HSC_DATABASE_PATH; else process.env.HSC_DATABASE_PATH = old }
})
