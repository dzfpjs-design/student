import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  userDataPath: '',
  isEncryptionAvailable: vi.fn(() => true),
  encryptString: vi.fn((value: string) => Buffer.from(`protected:${value}`, 'utf8')),
  decryptString: vi.fn((value: Buffer) => {
    const decoded = value.toString('utf8')
    if (!decoded.startsWith('protected:')) throw new Error('synthetic protected value rejected')
    return decoded.slice('protected:'.length)
  }),
}))
vi.mock('electron', () => ({
  app: { getPath: () => electron.userDataPath },
  safeStorage: {
    isEncryptionAvailable: electron.isEncryptionAvailable,
    encryptString: electron.encryptString,
    decryptString: electron.decryptString,
  },
}))
vi.mock('./windows-protected-startup', () => ({ waitForWindowsProtectedStorage: vi.fn() }))
import { ModelSettingsStore, type ModelConfig } from './model-settings'

// Only synthetic files are created. OS encryption is mocked, as in model-settings.test.ts.
const config: ModelConfig = {
  provider: 'qwen', apiKey: 'synthetic-primary-key', baseUrl: 'https://primary.example/v1',
  model: 'public-test-model', voiceIntentModel: 'public-intent-model',
  fallback: { provider: 'qwen', apiKey: 'synthetic-fallback-key', baseUrl: 'https://fallback.example/v1', model: 'public-test-model' },
}
const privateFailure = 'synthetic-private-exception-cipher-and-path-do-not-export'

describe('ModelSettingsStore load diagnostics', () => {
  let directory = ''
  let file = ''
  let store: ModelSettingsStore
  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), 'hsc-model-load-status-'))
    file = join(directory, 'model-settings.json')
    electron.userDataPath = directory
    electron.isEncryptionAvailable.mockReset().mockReturnValue(true)
    electron.encryptString.mockClear()
    electron.decryptString.mockReset().mockImplementation((value: Buffer) => {
      const decoded = value.toString('utf8')
      if (!decoded.startsWith('protected:')) throw new Error(privateFailure)
      return decoded.slice('protected:'.length)
    })
    store = new ModelSettingsStore(directory)
  })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }); vi.restoreAllMocks() })

  async function saved() {
    await store.save(config)
    electron.isEncryptionAvailable.mockClear()
    electron.encryptString.mockClear()
    electron.decryptString.mockClear()
    return readFile(file)
  }
  async function expectUnchanged(before: Buffer) {
    expect(await readFile(file)).toEqual(before)
    expect((await readdir(directory)).filter(name => name.endsWith('.tmp'))).toEqual([])
    expect(electron.encryptString).not.toHaveBeenCalled()
  }

  it('distinguishes a missing file from failure and keeps legacy null without touching storage', async () => {
    electron.isEncryptionAvailable.mockReturnValue(false)
    expect(await store.loadWithStatus()).toEqual({ config: null, diagnostic: 'not_configured' })
    expect(await store.load()).toBeNull()
    expect(electron.isEncryptionAvailable).not.toHaveBeenCalled()
    expect(electron.decryptString).not.toHaveBeenCalled()
    expect(await readdir(directory)).toEqual([])
  })

  it('reports a real read failure without treating an existing unreadable target as absent', async () => {
    await mkdir(file)
    expect(await store.loadWithStatus()).toEqual({ config: null, diagnostic: 'read_failed' })
    expect(await store.load()).toBeNull()
    expect((await stat(file)).isDirectory()).toBe(true)
    expect(electron.isEncryptionAvailable).not.toHaveBeenCalled()
    expect(electron.decryptString).not.toHaveBeenCalled()
  })

  it('reports real JSON parse failure before any storage check and preserves the file', async () => {
    const damaged = Buffer.from(`{${privateFailure}`)
    await writeFile(file, damaged)
    electron.isEncryptionAvailable.mockImplementation(() => { throw Error(privateFailure) })
    expect(await store.loadWithStatus()).toEqual({ config: null, diagnostic: 'parse_failed' })
    expect(await store.load()).toBeNull()
    expect(electron.isEncryptionAvailable).not.toHaveBeenCalled()
    expect(electron.decryptString).not.toHaveBeenCalled()
    await expectUnchanged(damaged)
  })

  for (const throws of [false, true]) it(`reports security storage ${throws ? 'throwing' : 'unavailable'} without retry or decrypt`, async () => {
    const before = await saved()
    electron.isEncryptionAvailable.mockImplementation(() => { if (throws) throw Error(privateFailure); return false })
    expect(await store.loadWithStatus()).toEqual({ config: null, diagnostic: 'secure_storage_unavailable' })
    expect(electron.isEncryptionAvailable).toHaveBeenCalledTimes(1)
    expect(await store.load()).toBeNull()
    expect(electron.isEncryptionAvailable).toHaveBeenCalledTimes(2)
    expect(electron.decryptString).not.toHaveBeenCalled()
    await expectUnchanged(before)
  })

  it('stops at primary decrypt failure without attempting fallback or exposing the exception', async () => {
    const before = await saved()
    electron.decryptString.mockImplementation(() => { throw Error(privateFailure) })
    const result = await store.loadWithStatus()
    expect(result).toEqual({ config: null, diagnostic: 'config_unreadable' })
    expect(JSON.stringify(result)).not.toContain(privateFailure)
    expect(electron.decryptString).toHaveBeenCalledTimes(1)
    expect(await store.load()).toBeNull()
    expect(electron.decryptString).toHaveBeenCalledTimes(2)
    expect(electron.isEncryptionAvailable).toHaveBeenCalledTimes(2)
    await expectUnchanged(before)
  })

  it('keeps fallback decrypt failure as legacy null instead of silently returning only the primary', async () => {
    const before = await saved()
    electron.decryptString.mockImplementation((value: Buffer) => {
      const decoded = value.toString('utf8')
      if (decoded === `protected:${config.fallback!.apiKey}`) throw Error(privateFailure)
      return decoded.slice('protected:'.length)
    })
    expect(await store.loadWithStatus()).toEqual({ config: null, diagnostic: 'config_unreadable' })
    expect(electron.decryptString).toHaveBeenCalledTimes(2)
    expect(await store.load()).toBeNull()
    expect(electron.decryptString).toHaveBeenCalledTimes(4)
    await expectUnchanged(before)
  })

  for (const fallback of [false, true]) it(`loads the real saved ${fallback ? 'primary and fallback' : 'primary'} config once per reached decrypt`, async () => {
    const current = fallback ? config : { ...config, fallback: undefined }
    await store.save(current)
    const before = await readFile(file)
    electron.isEncryptionAvailable.mockClear(); electron.encryptString.mockClear(); electron.decryptString.mockClear()
    expect(await store.loadWithStatus()).toEqual({ config: current, diagnostic: 'loaded' })
    expect(electron.decryptString).toHaveBeenCalledTimes(fallback ? 2 : 1)
    expect(await store.load()).toEqual(current)
    expect(electron.decryptString).toHaveBeenCalledTimes(fallback ? 4 : 2)
    expect(electron.isEncryptionAvailable).toHaveBeenCalledTimes(2)
    await expectUnchanged(before)
  })

  it('does not keep a previous failure after an explicit save and later successful read on the same store', async () => {
    await writeFile(file, '{bad-json')
    expect((await store.loadWithStatus()).diagnostic).toBe('parse_failed')
    await store.save(config)
    expect(await store.loadWithStatus()).toEqual({ config, diagnostic: 'loaded' })
  })
})
