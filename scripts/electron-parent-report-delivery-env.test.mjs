import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

const base = join(tmpdir(), 'synthetic-parent-delivery-env')
const fixture = { root: join(base, 'repo'), pythonBin: join(base, 'python'),
  dataDir: join(base, 'data'), databasePath: join(base, 'data', 'report.db'), profile: join(base, 'profile') }
const inherited = {
  PATH: process.env.PATH, LANG: 'zh_CN.UTF-8',
  HSC_LOCAL_AGENT_PYTHON: join(base, 'other-python'), HSC_LOCAL_MODEL_DIR: join(base, 'approved-models'),
  HSC_DATA_DIR: 'do-not-forward-family-data', HSC_DATABASE_PATH: 'do-not-forward-family-db',
  HSC_ELECTRON_USER_DATA_DIR: 'do-not-forward-family-profile', HSC_LOCAL_API_URL: 'http://unrelated.invalid',
  HSC_SESSION_TOKEN: 'synthetic-secret', HSC_DATA_ENCRYPTION_KEY: 'synthetic-secret',
  HSC_ASR_API_KEY: 'synthetic-secret', HSC_ASR_KEYCHAIN_SERVICE: 'family-asr', HSC_MODEL_KEYCHAIN_SERVICE: 'family-model',
  HSC_MODEL_API_KEY: 'synthetic-secret', HSC_FALLBACK_MODEL_API_KEY: 'synthetic-secret', HSC_TTS_API_KEY: 'synthetic-secret',
  HSC_DOCUMENT_VISION_API_KEY: 'synthetic-secret', HSC_REPORT_DELIVERY_EMAIL_PASSWORD: 'synthetic-secret',
  HSC_PARENT_PIN: 'synthetic-pin', HSC_FUTURE_SETTING: 'unknown-family-setting',
  OPENAI_API_KEY: 'synthetic-secret', DASHSCOPE_API_KEY: 'synthetic-secret', AWS_SESSION_TOKEN: 'synthetic-secret',
  GOOGLE_APPLICATION_CREDENTIALS: 'synthetic-credential-file'
}

test('report Electron environment strips inherited secrets and restores only its explicit fixture', () => {
  const original = { ...inherited }
  const result = parentReportDeliveryEnvironment(fixture, inherited)
  assert.deepEqual(result, { PATH: inherited.PATH, LANG: inherited.LANG,
    HSC_LOCAL_MODEL_DIR: inherited.HSC_LOCAL_MODEL_DIR, PYTHONPATH: join(fixture.root, 'services', 'local_agent', 'src'),
    HSC_LOCAL_AGENT_PYTHON: fixture.pythonBin, HSC_DATA_DIR: fixture.dataDir,
    HSC_DATABASE_PATH: fixture.databasePath, HSC_ELECTRON_USER_DATA_DIR: fixture.profile })
  assert.deepEqual(inherited, original)
})

test('seed process uses the same boundary and never inherits an Electron profile', () => {
  const { profile, ...seed } = fixture
  const result = parentReportDeliveryEnvironment(seed, inherited)
  assert.equal(result.HSC_ELECTRON_USER_DATA_DIR, undefined)
  assert.equal(result.HSC_DATABASE_PATH, fixture.databasePath)
  assert.equal(result.HSC_DATA_DIR, fixture.dataDir)
  assert.equal(result.HSC_ASR_KEYCHAIN_SERVICE, undefined)
})

test('arbitrary fixture fields cannot reintroduce provider or keychain credentials', () => {
  const result = parentReportDeliveryEnvironment({ ...fixture,
    HSC_ASR_KEYCHAIN_SERVICE: 'synthetic-bcf7690127d5b0c019c22e04', OPENAI_API_KEY: 'synthetic-bcf7690127d5b0c019c22e04' }, inherited)
  assert.equal(result.HSC_ASR_KEYCHAIN_SERVICE, undefined)
  assert.equal(result.OPENAI_API_KEY, undefined)
})

for (const field of ['root', 'pythonBin', 'dataDir', 'databasePath']) {
  test(`missing explicit ${field} fails before launching an unscoped process`, () => {
    assert.throws(() => parentReportDeliveryEnvironment({ ...fixture, [field]: undefined }, inherited), TypeError)
  })
}

test('an actual Node child receives only the constructed HSC scope, without running Python/Electron/SMTP', () => {
  const environment = parentReportDeliveryEnvironment(fixture, inherited)
  const result = JSON.parse(execFileSync(process.execPath, ['-e',
    "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(process.env).filter(([k]) => k.startsWith('HSC_') || k === 'OPENAI_API_KEY' || k === 'DASHSCOPE_API_KEY'))))"],
  { env: environment, encoding: 'utf8' }))
  assert.deepEqual(result, { HSC_LOCAL_MODEL_DIR: inherited.HSC_LOCAL_MODEL_DIR, HSC_LOCAL_AGENT_PYTHON: fixture.pythonBin,
    HSC_DATA_DIR: fixture.dataDir, HSC_DATABASE_PATH: fixture.databasePath, HSC_ELECTRON_USER_DATA_DIR: fixture.profile })
})

test('both report launcher boundaries use the tested factory, without the obsolete prefix filter', async () => {
  const source = await readFile(new URL('./electron-parent-report-delivery-e2e.mjs', import.meta.url), 'utf8')
  assert.match(source, /import \{ parentReportDeliveryEnvironment \} from '\.\/electron-parent-report-delivery-env\.mjs'/)
  assert.equal((source.match(/env: parentReportDeliveryEnvironment\(/g) ?? []).length, 2)
  assert.doesNotMatch(source, /cleanEnvironment|\.\.\.process\.env/)
})
