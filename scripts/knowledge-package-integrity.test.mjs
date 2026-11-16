import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { closeSync, createReadStream, ftruncateSync, mkdtempSync, openSync, readFileSync, rmSync, writeSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const script = join(dirname(fileURLToPath(import.meta.url)), 'stage-knowledge-package.mjs')
async function sha256(path) {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(path)) digest.update(chunk)
  return digest.digest('hex')
}
test('staging repairs same-size corruption and reuses only a verified output', async (t) => {
  const root = mkdtempSync(join(tmpdir(), 'hsc-knowledge-integrity-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const source = join(root, 'source.db')
  const fd = openSync(source, 'w')
  try { writeSync(fd, Buffer.from('SQLite format 3\0')); ftruncateSync(fd, 100_000_001) } finally { closeSync(fd) }
  const run = () => spawnSync(process.execPath, [script], {
    cwd: root, env: { ...process.env, HSC_ATOMIC_GRAPH_SOURCE: source }, encoding: 'utf8', timeout: 60000,
  })
  let result = run()
  assert.equal(result.status, 0, result.stderr)
  assert.equal(JSON.parse(result.stdout).staged, true)
  const target = join(root, 'dist/knowledge/atomic-knowledge.db')
  const manifestPath = join(root, 'dist/knowledge/atomic-knowledge-manifest.json')
  const output = openSync(target, 'r+')
  try { writeSync(output, Buffer.from([1]), 0, 1, 5000) } finally { closeSync(output) }
  assert.notEqual(await sha256(target), JSON.parse(readFileSync(manifestPath)).sha256)
  result = run()
  assert.equal(result.status, 0, result.stderr)
  assert.equal(JSON.parse(result.stdout).staged, true, 'same-size corruption must be repaired')
  assert.equal(await sha256(target), JSON.parse(readFileSync(manifestPath)).sha256)
  result = run()
  assert.equal(result.status, 0, result.stderr)
  assert.equal(JSON.parse(result.stdout).staged, false, 'a verified copy may be reused')
})
