import { app, safeStorage } from 'electron'
import { execFile } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { link, open, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { applyQwenOverlay, qwenOverlayTombstoned, QwenOverlayError, writeQwenOverlayEdit } from './qwen-key-overlay'
import { promisify } from 'node:util'
import { waitForWindowsProtectedStorage } from './windows-protected-startup'
import { assertFreshMediaProfile, MediaKeyRecoveryRequiredError } from './protected-media-profile'

const execFileAsync = promisify(execFile)

export type ModelEndpointConfig = {
  provider: 'qwen' | 'deepseek'
  apiKey: string
  baseUrl: string
  model: string
}

export type ModelConfig = ModelEndpointConfig & {
  voiceIntentModel: string
  voiceIntentApiKey?: string
  fallback?: ModelEndpointConfig
}

export type ModelLoadDiagnostic = 'loaded' | 'not_configured' | 'read_failed' | 'parse_failed' | 'secure_storage_unavailable' | 'config_unreadable'
export type ModelLoadResult = { config: ModelConfig | null; diagnostic: ModelLoadDiagnostic }

type DashScopeCredentialSource = {
  provider: 'dashscope'
  apiKey: string
  baseUrl: string
  model: string
}

type StoredEndpointConfig = Omit<ModelEndpointConfig, 'apiKey'> & { encryptedApiKey: string }
type StoredModelConfig = StoredEndpointConfig & { voiceIntentModel?: string; encryptedVoiceIntentApiKey?: string; fallback?: StoredEndpointConfig }

export class ModelSettingsStore {
  private readonly path: string

  constructor(userDataPath = app.getPath('userData')) {
    this.path = join(userDataPath, 'model-settings.json')
  }

  async load(): Promise<ModelConfig | null> {
    return (await this.loadWithStatus()).config
  }

  async loadWithStatus(): Promise<ModelLoadResult> {
    let diagnostic: ModelLoadDiagnostic = 'read_failed'
    try {
      const original = await readFile(this.path).catch(error => { if (isFileNotFound(error)) return null; throw error })
      const bytes = applyQwenOverlay(dirname(this.path), 'model-settings.json', original)
      if (!bytes) return { config: null, diagnostic: 'not_configured' }
      const raw = bytes.toString('utf8')
      diagnostic = 'parse_failed'
      const stored = JSON.parse(raw) as StoredModelConfig
      diagnostic = 'secure_storage_unavailable'
      if (!safeStorage.isEncryptionAvailable()) return { config: null, diagnostic }
      diagnostic = 'config_unreadable'
      if (Object.hasOwn(stored, 'encryptedVoiceIntentApiKey') && (typeof stored.encryptedVoiceIntentApiKey !== 'string'
        || !stored.encryptedVoiceIntentApiKey || Buffer.from(stored.encryptedVoiceIntentApiKey, 'base64').toString('base64') !== stored.encryptedVoiceIntentApiKey)) throw Error('INTENT_CREDENTIAL_INVALID')
      const config = {
        provider: stored.provider,
        apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')),
        baseUrl: stored.baseUrl,
        model: stored.model,
        voiceIntentModel: stored.voiceIntentModel || stored.model,
        ...(stored.encryptedVoiceIntentApiKey ? { voiceIntentApiKey: safeStorage.decryptString(Buffer.from(stored.encryptedVoiceIntentApiKey, 'base64')) } : {}),
        ...(stored.fallback ? {
          fallback: {
            provider: stored.fallback.provider,
            apiKey: safeStorage.decryptString(Buffer.from(stored.fallback.encryptedApiKey, 'base64')),
            baseUrl: stored.fallback.baseUrl,
            model: stored.fallback.model
          }
        } : {})
      }
      return { config, diagnostic: 'loaded' }
    } catch (error) {
      if (error instanceof QwenOverlayError) throw error
      if (diagnostic === 'read_failed') {
        // Error metadata is used only to distinguish an absent file; nothing is exported.
        try { if (isFileNotFound(error)) diagnostic = 'not_configured' } catch { /* keep read_failed */ }
      }
      return { config: null, diagnostic }
    }
  }

  async save(config: ModelConfig): Promise<void> {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('当前系统安全存储不可用，未保存模型密钥。')
    }
    const normalized = validateModelConfig(config)
    const stored: StoredModelConfig = {
      provider: normalized.provider,
      baseUrl: normalized.baseUrl,
      model: normalized.model,
      voiceIntentModel: normalized.voiceIntentModel,
      encryptedApiKey: safeStorage.encryptString(normalized.apiKey).toString('base64'),
      ...(normalized.voiceIntentApiKey ? { encryptedVoiceIntentApiKey: safeStorage.encryptString(normalized.voiceIntentApiKey).toString('base64') } : {}),
      ...(normalized.fallback ? {
        fallback: {
          provider: normalized.fallback.provider,
          baseUrl: normalized.fallback.baseUrl,
          model: normalized.fallback.model,
          encryptedApiKey: safeStorage.encryptString(normalized.fallback.apiKey).toString('base64')
        }
      } : {})
    }
    if (writeQwenOverlayEdit(dirname(this.path), 'model-settings.json', Buffer.from(JSON.stringify(stored)))) return
    const temporaryPath = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try {
      await writeFile(temporaryPath, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
      await rename(temporaryPath, this.path)
    } finally {
      await unlink(temporaryPath).catch(() => undefined)
    }
  }

  async loadOrImportDeepSeekFromMacOSKeychain(service: string): Promise<ModelConfig | null> {
    const existing = await this.load()
    if (existing) return existing
    if (qwenOverlayTombstoned(dirname(this.path), 'model-settings.json')) return null
    if (process.platform !== 'darwin' || !service.trim()) return null
    if (!safeStorage.isEncryptionAvailable()) return null
    const { stdout } = await execFileAsync('/usr/bin/security', [
      'find-generic-password', '-s', service.trim(), '-w',
    ], { encoding: 'utf8', maxBuffer: 8_192 })
    const apiKey = stdout.trim()
    if (apiKey.length < 8) throw new Error('macOS 钥匙串中的模型密钥格式不完整。')
    const imported = validateModelConfig({
      provider: 'deepseek', apiKey, baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-v4-pro', voiceIntentModel: 'deepseek-v4-flash',
    })
    await this.save(imported)
    return imported
  }

  async clear(): Promise<void> {
    if (writeQwenOverlayEdit(dirname(this.path), 'model-settings.json', null)) return
    try {
      await unlink(this.path)
    } catch (error) {
      if (!isFileNotFound(error)) throw error
    }
  }
}

const pendingMediaKeys = new Map<string, Promise<string>>()

export class DataEncryptionKeyStore {
  private readonly userDataPath = app.getPath('userData')
  private readonly path = join(this.userDataPath, 'data-encryption-key.bin')

  async loadOrCreate(): Promise<string> {
    const pending = pendingMediaKeys.get(this.path)
    if (pending) return pending
    const request = this.loadOrCreateProtectedKey()
    pendingMediaKeys.set(this.path, request)
    try {
      return await request
    } finally {
      if (pendingMediaKeys.get(this.path) === request) pendingMediaKeys.delete(this.path)
    }
  }

  private async readProtectedKey(): Promise<string> {
    const encoded = safeStorage.decryptString(await readFile(this.path))
    const key = Buffer.from(encoded, 'base64')
    if (key.length !== 32 || key.toString('base64') !== encoded) {
      throw new Error('本地媒体加密密钥格式无效。')
    }
    return encoded
  }

  private async loadOrCreateProtectedKey(): Promise<string> {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('当前系统安全存储不可用，无法启动受保护的本地媒体存储。')
    }

    if (process.platform === 'win32') await waitForWindowsProtectedStorage(app.getPath('sessionData'))

    try {
      return await this.readProtectedKey()
    } catch (error) {
      if (!isFileNotFound(error)) {
        throw new MediaKeyRecoveryRequiredError('无法读取本地媒体加密密钥；为保护历史数据，系统不会自动换钥。', error)
      }
    }

    await assertFreshMediaProfile(this.userDataPath)
    const encoded = randomBytes(32).toString('base64')
    const temporaryPath = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try {
      const file = await open(temporaryPath, 'wx', 0o600)
      try {
        await file.writeFile(safeStorage.encryptString(encoded))
        await file.sync()
      } finally {
        await file.close()
      }
      // Publish a complete blob without replacing a competing process's key.
      try {
        await link(temporaryPath, this.path)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      }
      return await this.readProtectedKey()
    } catch (error) {
      throw new MediaKeyRecoveryRequiredError('无法安全保存本地媒体密钥；系统不会自动换钥，请保留现有档案后重试。', error)
    } finally {
      await unlink(temporaryPath).catch(() => undefined)
    }
  }
}

export type ModelSettingsSmokeResult = {
  safe_storage_available: boolean
  encrypted_round_trip: boolean
  plaintext_absent: boolean
  credentials_cleared: boolean
}

export async function runModelSettingsSmoke(userDataPath: string): Promise<ModelSettingsSmokeResult> {
  if (!safeStorage.isEncryptionAvailable()) {
    return { safe_storage_available: false, encrypted_round_trip: false, plaintext_absent: false, credentials_cleared: false }
  }
  const syntheticPrimary = `smoke-primary-${randomBytes(16).toString('hex')}`
  const syntheticFallback = `smoke-fallback-${randomBytes(16).toString('hex')}`
  const store = new ModelSettingsStore(userDataPath)
  const path = join(userDataPath, 'model-settings.json')
  try {
    await store.save({
      provider: 'qwen', apiKey: syntheticPrimary, baseUrl: 'https://model-smoke.invalid/v1',
      model: 'qwen-smoke', voiceIntentModel: 'qwen-smoke-intent',
      fallback: { provider: 'deepseek', apiKey: syntheticFallback, baseUrl: 'https://fallback-smoke.invalid/v1', model: 'deepseek-smoke' },
    })
    const raw = await readFile(path, 'utf8')
    const loaded = await store.load()
    const encryptedRoundTrip = loaded?.apiKey === syntheticPrimary && loaded.fallback?.apiKey === syntheticFallback
    const plaintextAbsent = !raw.includes(syntheticPrimary) && !raw.includes(syntheticFallback)
    await store.clear()
    const credentialsCleared = await store.load() === null
    return { safe_storage_available: true, encrypted_round_trip: encryptedRoundTrip, plaintext_absent: plaintextAbsent, credentials_cleared: credentialsCleared }
  } finally {
    await store.clear().catch(() => undefined)
  }
}

function isFileNotFound(error: unknown): boolean {
  return Boolean(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')
}

export function validateModelConfig(config: ModelConfig): ModelConfig {
  const primary = validateModelEndpoint(config)
  const voiceIntentModel = validateModelName(config.voiceIntentModel, '实时意图模型')
  const voiceIntentApiKey = config.voiceIntentApiKey?.trim()
  if (voiceIntentApiKey !== undefined && voiceIntentApiKey.length < 8) throw new Error('实时意图密钥格式不完整。')
  const fallback = config.fallback ? validateModelEndpoint(config.fallback) : undefined
  if (fallback && sameModelTarget(primary, fallback)) {
    throw new Error('备用模型不能与首选模型完全相同。')
  }
  return { ...primary, voiceIntentModel, ...(voiceIntentApiKey ? { voiceIntentApiKey } : {}), ...(fallback ? { fallback } : {}) }
}

export function resolveModelConfigCredentials(config: ModelConfig, current: ModelConfig | null): ModelConfig {
  const primaryKey = config.apiKey.trim() || (current && sameProviderEndpoint(config, current) ? current.apiKey : '')
  const fallbackKey = config.fallback?.apiKey.trim() || (
    config.fallback && current?.fallback && sameProviderEndpoint(config.fallback, current.fallback) ? current.fallback.apiKey : ''
  )
  const voiceIntentApiKey = config.voiceIntentApiKey?.trim() || (current && sameProviderEndpoint(config, current)
    && config.voiceIntentModel === current.voiceIntentModel ? current.voiceIntentApiKey : undefined)
  return validateModelConfig({
    ...config,
    apiKey: primaryKey,
    voiceIntentApiKey,
    ...(config.fallback ? { fallback: { ...config.fallback, apiKey: fallbackKey } } : { fallback: undefined }),
  })
}

export function ensureDashScopeTextFallback(
  config: ModelConfig | null,
  dashscope: DashScopeCredentialSource | null,
): ModelConfig | null {
  if (!config || config.fallback || config.provider !== 'deepseek' || !dashscope?.apiKey.trim()) return config
  return validateModelConfig({
    ...config,
    fallback: {
      provider: 'qwen',
      apiKey: dashscope.apiKey,
      baseUrl: dashscope.baseUrl,
      model: 'qwen3.7-plus',
    },
  })
}

function sameProviderEndpoint(left: ModelEndpointConfig, right: ModelEndpointConfig): boolean {
  return left.provider === right.provider && normalizeBaseUrl(left.baseUrl) === normalizeBaseUrl(right.baseUrl)
}

function sameModelTarget(left: ModelEndpointConfig, right: ModelEndpointConfig): boolean {
  return sameProviderEndpoint(left, right) && left.model === right.model
}

function normalizeBaseUrl(value: string): string {
  try { return new URL(value.trim()).toString().replace(/\/+$/, '') } catch { return value.trim().replace(/\/+$/, '') }
}

function validateModelEndpoint(config: ModelEndpointConfig): ModelEndpointConfig {
  const apiKey = config.apiKey.trim()
  const model = validateModelName(config.model, '模型')
  if (apiKey.length < 8) throw new Error('API Key 格式不完整。')
  let url: URL
  try { url = new URL(config.baseUrl.trim()) } catch { throw new Error('API 地址格式无效。') }
  if (url.protocol !== 'https:' || url.username || url.password) {
    throw new Error('API 地址必须是不含账号信息的 HTTPS 地址。')
  }
  return { provider: config.provider, apiKey, baseUrl: url.toString().replace(/\/+$/, ''), model }
}

function validateModelName(value: string, label: string): string {
  const model = value.trim()
  if (!/^[a-zA-Z0-9._:-]{2,120}$/.test(model)) throw new Error(`${label}名称格式无效。`)
  return model
}
