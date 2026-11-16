import assert from 'node:assert/strict'
import test from 'node:test'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))

test('seed uses real HTTP reflection/report and never inserts completed learning or imports legacy fixture', async () => {
  const source = await readFile(new URL('./seed-parent-report-delivery.py', import.meta.url), 'utf8')
  assert.ok(source.includes('TestClient(create_app(settings))'))
  assert.ok(source.includes('/reflection/complete'))
  assert.ok(source.includes('get_report_for_session(sid)'))
  assert.ok(source.includes('create_from_daily(report)'))
  assert.ok(source.includes('completed_tasks == 0'))
  assert.ok(!/INSERT INTO|UPDATE |verify_parent_report_delivery_local|\.complete_plan_item\(/.test(source))
})

test('existing isolated database is refused before ASGI import, without altering its bytes', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'hsc-report-seed-guard-'))
  const databasePath = join(temp, 'existing.db')
  const pythonBin = join(root, '.venv/bin/python')
  const bytes = Buffer.from('synthetic preserved failure evidence')
  try {
    await writeFile(databasePath, bytes)
    const result = spawnSync(pythonBin, [join(root, 'scripts/seed-parent-report-delivery.py'),
      '--database-path', databasePath, '--receipt-path', join(temp, 'receipt.json')], {
      encoding: 'utf8', timeout: 15_000,
      env: parentReportDeliveryEnvironment({ root, pythonBin, dataDir: temp, databasePath }) })
    assert.equal(result.status, 1)
    assert.match(result.stderr, /existing evidence is never overwritten/)
    assert.deepEqual(await readFile(databasePath), bytes)
    await assert.rejects(readFile(join(temp, 'receipt.json')), { code: 'ENOENT' })
  } finally { await rm(temp, { recursive: true, force: true }) }
})

test('unbound invocation fails before module-level ASGI can create a different database', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'hsc-report-seed-unbound-'))
  const databasePath = join(temp, 'new.db')
  const pythonBin = join(root, '.venv/bin/python')
  try {
    const env = parentReportDeliveryEnvironment({ root, pythonBin, dataDir: temp, databasePath })
    delete env.HSC_DATABASE_PATH
    const result = spawnSync(pythonBin, [join(root, 'scripts/seed-parent-report-delivery.py'),
      '--database-path', databasePath, '--receipt-path', join(temp, 'receipt.json')],
    { encoding: 'utf8', timeout: 15_000, env })
    assert.equal(result.status, 1)
    assert.match(result.stderr, /environment bound to its explicit database path/)
    await assert.rejects(readFile(databasePath), { code: 'ENOENT' })
  } finally { await rm(temp, { recursive: true, force: true }) }
})
