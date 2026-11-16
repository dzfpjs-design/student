import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  encryptionAvailable: true,
  userDataPath: '',
  encryptString: vi.fn((value: string) => Buffer.from(`protected:${value}`, 'utf8')),
  decryptString: vi.fn((value: Buffer) => {
    const decoded = value.toString('utf8')
    if (!decoded.startsWith('protected:')) throw new Error('invalid protected value')
    return decoded.slice('protected:'.length)
  }),
}))

vi.mock('electron', () => ({
  app: { getPath: () => electron.userDataPath },
  safeStorage: {
    isEncryptionAvailable: () => electron.encryptionAvailable,
    encryptString: electron.encryptString,
    decryptString: electron.decryptString,
  },
}))

// This file tests key publication with mocked OS encryption. The real
// Windows provider persistence gate has its own filesystem and runtime tests.
vi.mock('./windows-protected-startup', () => ({ waitForWindowsProtectedStorage: vi.fn() }))

import { DataEncryptionKeyStore, ensureDashScopeTextFallback, ModelSettingsStore, resolveModelConfigCredentials, runModelSettingsSmoke, type ModelConfig } from './model-settings'

const config: ModelConfig = {
  provider: 'qwen',
  apiKey: 'synthetic-e14b411d1e4eacb4ae7a',
  baseUrl: 'https://model.example/v1',
  model: 'qwen3.7-plus',
  voiceIntentModel: 'qwen3.8-flash',
  fallback: {
    provider: 'qwen',
    apiKey: 'synthetic-d1d41750f93e9393abf7',
    baseUrl: 'https://fallback.example/v1',
    model: 'qwen3.7-plus',
  },
}

describe('ModelSettingsStore', () => {
  let directory = ''

  beforeEach(async () => {
    for (const name of ['HSC_DATA_DIR', 'HSC_DATABASE_PATH', 'HSC_RUNTIME_MODE', 'HSC_TRIAL_USER_DATA_DIR']) vi.stubEnv(name, undefined)
    directory = await mkdtemp(join(tmpdir(), 'hsc-model-settings-'))
    electron.userDataPath = directory
    electron.encryptionAvailable = true
    electron.encryptString.mockClear()
    electron.decryptString.mockClear()
  })

  afterEach(async () => {
    vi.unstubAllEnvs()
    await rm(directory, { recursive: true, force: true })
  })

  it('atomically replaces a damaged file without storing plaintext credentials', async () => {
    const path = join(directory, 'model-settings.json')
    await writeFile(path, '{damaged')
    const store = new ModelSettingsStore(directory)

    await store.save(config)

    const raw = await readFile(path, 'utf8')
    expect(raw).not.toContain(config.apiKey)
    expect(raw).not.toContain(config.fallback?.apiKey)
    expect(await store.load()).toEqual(config)
    expect((await readdir(directory)).filter((name) => name.endsWith('.tmp'))).toEqual([])
  })

  it('persists a distinct intent credential and preserves it only for the same route', async () => {
    const store = new ModelSettingsStore(directory), separated = { ...config, voiceIntentApiKey: 'synthetic-intent-secret' }
    await store.save(separated)
    expect((await readFile(join(directory, 'model-settings.json'),'utf8'))).not.toContain(separated.voiceIntentApiKey)
    expect(await store.load()).toEqual(separated)
    expect(resolveModelConfigCredentials({ ...separated, apiKey:'', voiceIntentApiKey:undefined },separated).voiceIntentApiKey).toBe(separated.voiceIntentApiKey)
    expect(resolveModelConfigCredentials({ ...separated, voiceIntentModel:'qwen-another', voiceIntentApiKey:undefined },separated).voiceIntentApiKey).toBeUndefined()
    await writeFile(join(directory,'model-settings.json'),JSON.stringify({...JSON.parse(await readFile(join(directory,'model-settings.json'),'utf8')),encryptedVoiceIntentApiKey:''}))
    expect((await store.loadWithStatus()).diagnostic).toBe('config_unreadable')
  })

  it('restores the same media key across store reconstruction, never replacing damaged data', async () => {
    const first = await new DataEncryptionKeyStore().loadOrCreate()
    expect(Buffer.from(first, 'base64')).toHaveLength(32)
    expect(await new DataEncryptionKeyStore().loadOrCreate()).toBe(first)
    const path = join(directory, 'data-encryption-key.bin')
    await writeFile(path, 'damaged-protected-blob')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(await readFile(path, 'utf8')).toBe('damaged-protected-blob')
  })

  it('refuses a replacement key beside existing learning data without changing the data', async () => {
    const data = join(directory, 'family-data')
    await mkdir(data)
    const database = join(data, 'home-study.db')
    await writeFile(database, 'synthetic-learning-data')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(await readFile(database, 'utf8')).toBe('synthetic-learning-data')
    expect(await readdir(directory)).not.toContain('data-encryption-key.bin')
    expect(electron.encryptString).not.toHaveBeenCalled()
  })

  it('refuses a replacement key when only old media remains', async () => {
    const media = join(directory, 'family-data', 'paper-pages')
    await mkdir(media, { recursive: true })
    await writeFile(join(media, 'synthetic.enc'), 'synthetic-encrypted-page')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(await readFile(join(media, 'synthetic.enc'), 'utf8')).toBe('synthetic-encrypted-page')
  })

  it('uses the same actual data paths as the backend instead of assuming the profile directory', async () => {
    const custom = join(directory, 'custom-data')
    await mkdir(custom)
    vi.stubEnv('HSC_DATA_DIR', custom)
    await writeFile(join(custom, 'home-study.db'), 'custom-synthetic-db')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
  })

  it('keeps first-run diagnostics compatible with initialization', async () => {
    const diagnostics = join(directory, 'family-data', 'diagnostics')
    await mkdir(diagnostics, { recursive: true })
    await writeFile(join(diagnostics, 'startup.jsonl'), 'synthetic-diagnostic')
    expect(Buffer.from(await new DataEncryptionKeyStore().loadOrCreate(), 'base64')).toHaveLength(32)
  })

  it('does not mistake an empty database or its sidecar for a fresh profile', async () => {
    const database = join(directory, 'external-db')
    vi.stubEnv('HSC_DATABASE_PATH', database)
    for (const suffix of ['', '-wal', '-shm', '-journal']) {
      await writeFile(database + suffix, '')
      await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
      await rm(database + suffix)
    }
    expect(electron.encryptString).not.toHaveBeenCalled()
  })

  it('preserves a stranded unpublished key instead of silently generating another', async () => {
    const path = join(directory, 'data-encryption-key.bin.interrupted.tmp')
    await writeFile(path, 'synthetic-incomplete-key')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(await readFile(path, 'utf8')).toBe('synthetic-incomplete-key')
  })

  it('coalesces concurrent stores and returns one durable key', async () => {
    const keys = await Promise.all(Array.from({ length: 8 }, () => new DataEncryptionKeyStore().loadOrCreate()))
    expect(new Set(keys).size).toBe(1)
    expect(electron.encryptString).toHaveBeenCalledTimes(1)
    expect((await readdir(directory)).filter(name => name.endsWith('.tmp'))).toEqual([])
    expect(await new DataEncryptionKeyStore().loadOrCreate()).toBe(keys[0])
  })

  it('restores an original valid key despite existing assets and never uses asset checks to rotate it', async () => {
    const key = await new DataEncryptionKeyStore().loadOrCreate()
    const protectedBlob = await readFile(join(directory, 'data-encryption-key.bin'))
    await mkdir(join(directory, 'family-data'))
    await writeFile(join(directory, 'family-data', 'home-study.db'), 'synthetic-db')
    await rm(join(directory, 'data-encryption-key.bin'))
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    await writeFile(join(directory, 'data-encryption-key.bin'), protectedBlob)
    expect(await new DataEncryptionKeyStore().loadOrCreate()).toBe(key)
  })

  it('rejects malformed encoded key material without overwriting the original file', async () => {
    const path = join(directory, 'data-encryption-key.bin')
    const invalid = 'protected:' + Buffer.alloc(32, 1).toString('base64') + '!'
    await writeFile(path, invalid)
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(await readFile(path, 'utf8')).toBe(invalid)
  })

  it('refuses unknown assets and a diagnostics path that is not a physical directory', async () => {
    const data = join(directory, 'family-data')
    await mkdir(data)
    for (const name of ['unrecognized-asset', 'diagnostics']) {
      await writeFile(join(data, name), '')
      await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
      await rm(join(data, name))
    }
    expect(electron.encryptString).not.toHaveBeenCalled()
  })

  it('refuses dangling database links and does not follow diagnostics links', async () => {
    const data = join(directory, 'family-data')
    await mkdir(data)
    const database = join(data, 'home-study.db')
    await symlink(join(directory, 'missing-db'), database, process.platform === 'win32' ? 'junction' : 'file')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    await rm(database)
    await symlink(directory, join(data, 'diagnostics'), process.platform === 'win32' ? 'junction' : 'dir')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
  })

  it('does not initialize ambiguous relative data paths', async () => {
    vi.stubEnv('HSC_DATA_DIR', 'relative-family-data')
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(electron.encryptString).not.toHaveBeenCalled()
  })

  it('respects trial path isolation before creating a key', async () => {
    vi.stubEnv('HSC_RUNTIME_MODE', 'trial')
    vi.stubEnv('HSC_TRIAL_USER_DATA_DIR', directory)
    vi.stubEnv('HSC_DATA_DIR', join(directory, '..', 'outside-profile'))
    await expect(new DataEncryptionKeyStore().loadOrCreate()).rejects.toThrow('不会自动换钥')
    expect(electron.encryptString).not.toHaveBeenCalled()
    vi.stubEnv('HSC_DATA_DIR', join(directory, 'family-data'))
    expect(Buffer.from(await new DataEncryptionKeyStore().loadOrCreate(), 'base64')).toHaveLength(32)
  })

  it('clears configured credentials idempotently', async () => {
    const store = new ModelSettingsStore(directory)
    await store.save(config)

    await store.clear()
    await store.clear()

    expect(await store.load()).toBeNull()
    expect(await readdir(directory)).toEqual([])
  })

  it('refuses to replace the current file when system encryption is unavailable', async () => {
    const path = join(directory, 'model-settings.json')
    await writeFile(path, 'existing-protected-settings')
    electron.encryptionAvailable = false

    await expect(new ModelSettingsStore(directory).save(config)).rejects.toThrow('系统安全存储不可用')
    expect(await readFile(path, 'utf8')).toBe('existing-protected-settings')
  })

  it('allows the same provider on a distinct fallback endpoint', async () => {
    const store = new ModelSettingsStore(directory)
    await store.save(config)

    expect(await store.load()).toEqual(config)
  })

  it('rejects an exact duplicate model target as a fallback', async () => {
    const store = new ModelSettingsStore(directory)
    await expect(store.save({
      ...config,
      fallback: { provider: config.provider, apiKey: 'synthetic-9c0ee26e4a1fbb028187', baseUrl: config.baseUrl, model: config.model },
    })).rejects.toThrow('备用模型不能与首选模型完全相同')
  })

  it('reuses protected credentials only while each provider endpoint remains unchanged', () => {
    const updated = resolveModelConfigCredentials({
      ...config,
      apiKey: '',
      model: 'qwen3.7-plus-new',
      fallback: { ...config.fallback!, apiKey: '', model: 'qwen3.7-plus-new' },
    }, config)

    expect(updated.apiKey).toBe(config.apiKey)
    expect(updated.fallback?.apiKey).toBe(config.fallback?.apiKey)
    expect(() => resolveModelConfigCredentials({ ...config, provider: 'deepseek', apiKey: '', fallback: undefined }, config)).toThrow('API Key 格式不完整')
    expect(() => resolveModelConfigCredentials({
      ...config,
      fallback: { ...config.fallback!, baseUrl: 'https://another.example/v1', apiKey: '' },
    }, config)).toThrow('API Key 格式不完整')
  })

  it('adds the existing paid DashScope account as a durable text fallback without replacing DeepSeek', () => {
    const primary: ModelConfig = {
      provider: 'deepseek', apiKey: 'synthetic-caecf716aa293fd7d109', baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-v4-pro', voiceIntentModel: 'deepseek-v4-flash',
    }
    const withFallback = ensureDashScopeTextFallback(primary, {
      provider: 'dashscope', apiKey: 'synthetic-65f15aaab2f906421fb2',
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-asr-flash',
    })

    expect(withFallback).toEqual({
      ...primary,
      fallback: {
        provider: 'qwen', apiKey: 'synthetic-65f15aaab2f906421fb2',
        baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3.7-plus',
      },
    })
    expect(ensureDashScopeTextFallback(withFallback, null)).toBe(withFallback)
  })

  it('runs a credential-free target-runtime smoke and removes its synthetic settings', async () => {
    expect(await runModelSettingsSmoke(directory)).toEqual({
      safe_storage_available: true,
      encrypted_round_trip: true,
      plaintext_absent: true,
      credentials_cleared: true,
    })
    expect(await readdir(directory)).toEqual([])
  })
})
