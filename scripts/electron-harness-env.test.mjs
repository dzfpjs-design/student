import test from 'node:test'
import assert from 'node:assert/strict'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'

test('removes inherited providers and isolated-runtime state while retaining ordinary environment', () => {
  const result = sanitizedHarnessEnvironment({
    PATH: '/safe/bin', LANG: 'zh_CN.UTF-8', HSC_LOCAL_AGENT_PYTHON: '/approved/python', HSC_LOCAL_MODEL_DIR: '/approved/models',
    HSC_MODEL_API_KEY: 'synthetic-aa21e9cb57e955686c7a', HSC_TTS_API_KEY: 'synthetic-aa21e9cb57e955686c7a', HSC_REPORT_DELIVERY_EMAIL_PASSWORD: 'synthetic-aa21e9cb57e955686c7a',
    OPENAI_API_KEY: 'synthetic-aa21e9cb57e955686c7a', GOOGLE_APPLICATION_CREDENTIALS: '/host/credentials.json', HSC_LOCAL_API_URL: 'http://127.0.0.1:9999', HSC_SESSION_TOKEN: 'synthetic-aa21e9cb57e955686c7a'
  })

  assert.equal(result.PATH, '/safe/bin')
  assert.equal(result.LANG, 'zh_CN.UTF-8')
  assert.equal(result.HSC_LOCAL_AGENT_PYTHON, '/approved/python')
  assert.equal(result.HSC_LOCAL_MODEL_DIR, '/approved/models')
  for (const key of ['HSC_MODEL_API_KEY', 'HSC_TTS_API_KEY', 'HSC_REPORT_DELIVERY_EMAIL_PASSWORD', 'OPENAI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'HSC_LOCAL_API_URL', 'HSC_SESSION_TOKEN']) assert.equal(result[key], undefined)
})

test('does not import family paths, audio credentials, or keychain configuration into an isolated harness', () => {
  const inherited = {
    PATH: '/safe/bin', HSC_LOCAL_AGENT_PYTHON: '/approved/python', HSC_LOCAL_MODEL_DIR: '/approved/models',
    HSC_DATA_DIR: '/family/data', HSC_DATABASE_PATH: '/family/data/family.db',
    HSC_ELECTRON_USER_DATA_DIR: '/family/profile', HSC_DATA_ENCRYPTION_KEY: 'synthetic-secret',
    HSC_ASR_API_KEY: 'synthetic-secret', HSC_ASR_KEYCHAIN_SERVICE: 'family-speech',
    HSC_DOCUMENT_VISION_API_KEY: 'synthetic-secret', HSC_MODEL_KEYCHAIN_SERVICE: 'family-model',
    HSC_PARENT_PIN: 'synthetic-parent', HSC_FUTURE_CREDENTIAL: 'synthetic-future-secret'
  }
  const result = sanitizedHarnessEnvironment(inherited)
  assert.deepEqual(result, {
    PATH: '/safe/bin', HSC_LOCAL_AGENT_PYTHON: '/approved/python', HSC_LOCAL_MODEL_DIR: '/approved/models'
  })
  assert.equal(inherited.HSC_DATA_DIR, '/family/data')
})
