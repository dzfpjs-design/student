import { app, safeStorage } from 'electron'
import { execFile } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { applyQwenOverlay, qwenOverlayTombstoned, QwenOverlayError, writeQwenOverlayEdit } from './qwen-key-overlay'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

export type CloudAsrConfig = {
  provider: 'dashscope'
  apiKey: string
  baseUrl: string
  model: string
  documentVisionApiKey?: string
}

type StoredCloudAsrConfig = Omit<CloudAsrConfig, 'apiKey' | 'documentVisionApiKey'> & { encryptedApiKey: string; encryptedDocumentVisionApiKey?: string }

export class CloudAsrSettingsStore {
  private readonly path: string

  constructor(userDataPath = app.getPath('userData')) {
    this.path = join(userDataPath, 'asr-settings.json')
  }

  async load(): Promise<CloudAsrConfig | null> {
    try {
      const original = await readFile(this.path).catch(error => { if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') return null; throw error })
      const bytes = applyQwenOverlay(dirname(this.path), 'asr-settings.json', original)
      if (!bytes) return null
      const stored = JSON.parse(bytes.toString('utf8')) as StoredCloudAsrConfig
      if (!safeStorage.isEncryptionAvailable()) return null
      if (Object.hasOwn(stored,'encryptedDocumentVisionApiKey') && (typeof stored.encryptedDocumentVisionApiKey !== 'string'
        || !stored.encryptedDocumentVisionApiKey || Buffer.from(stored.encryptedDocumentVisionApiKey,'base64').toString('base64') !== stored.encryptedDocumentVisionApiKey)) throw Error('DOCUMENT_VISION_CREDENTIAL_INVALID')
      return validateCloudAsrConfig({
        provider: stored.provider,
        apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')),
        baseUrl: stored.baseUrl,
        model: stored.model,
        ...(stored.encryptedDocumentVisionApiKey ? { documentVisionApiKey: safeStorage.decryptString(Buffer.from(stored.encryptedDocumentVisionApiKey,'base64')) } : {}),
      })
    } catch (error) {
      if (error instanceof QwenOverlayError) throw error
      return null
    }
  }

  async save(config: CloudAsrConfig): Promise<CloudAsrConfig> {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('当前系统安全存储不可用，未保存语音识别密钥。')
    const previous = await this.load()
    const documentVisionApiKey = config.documentVisionApiKey?.trim() || (previous?.baseUrl === config.baseUrl ? previous.documentVisionApiKey : undefined)
    const normalized = validateCloudAsrConfig({ ...config, documentVisionApiKey })
    const stored: StoredCloudAsrConfig = {
      provider: normalized.provider,
      baseUrl: normalized.baseUrl,
      model: normalized.model,
      encryptedApiKey: safeStorage.encryptString(normalized.apiKey).toString('base64'),
      ...(normalized.documentVisionApiKey ? { encryptedDocumentVisionApiKey: safeStorage.encryptString(normalized.documentVisionApiKey).toString('base64') } : {}),
    }
    if (writeQwenOverlayEdit(dirname(this.path), 'asr-settings.json', Buffer.from(JSON.stringify(stored)))) return normalized
    const temporaryPath = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try {
      await writeFile(temporaryPath, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
      await rename(temporaryPath, this.path)
    } finally {
      await unlink(temporaryPath).catch(() => undefined)
    }
    return normalized
  }

  async loadOrImportDashScopeFromMacOSKeychain(service: string): Promise<CloudAsrConfig | null> {
    const existing = await this.load()
    if (existing) return existing
    if (qwenOverlayTombstoned(dirname(this.path), 'asr-settings.json')) return null
    if (process.platform !== 'darwin' || !service.trim() || !safeStorage.isEncryptionAvailable()) return null
    const { stdout } = await execFileAsync('/usr/bin/security', [
      'find-generic-password', '-s', service.trim(), '-w',
    ], { encoding: 'utf8', maxBuffer: 8_192 })
    return this.save({
      provider: 'dashscope',
      apiKey: stdout.trim(),
      baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: 'qwen3-asr-flash',
    })
  }

  async clear(): Promise<void> {
    if (writeQwenOverlayEdit(dirname(this.path), 'asr-settings.json', null)) return
    await unlink(this.path).catch((error: unknown) => {
      if (!error || typeof error !== 'object' || !('code' in error) || error.code !== 'ENOENT') throw error
    })
  }
}

export function validateCloudAsrConfig(config: CloudAsrConfig): CloudAsrConfig {
  const apiKey = config.apiKey.trim()
  if (apiKey.length < 8) throw new Error('ASR API Key 格式不完整。')
  const documentVisionApiKey = config.documentVisionApiKey?.trim()
  if (documentVisionApiKey !== undefined && documentVisionApiKey.length < 8) throw new Error('文档视觉密钥格式不完整。')
  let url: URL
  try { url = new URL(config.baseUrl.trim()) } catch { throw new Error('ASR API 地址格式无效。') }
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('ASR API 地址必须是不含账号信息的 HTTPS 地址。')
  const model = config.model.trim()
  if (!/^[a-zA-Z0-9._:-]{2,120}$/.test(model)) throw new Error('ASR 模型名称格式无效。')
  return { provider: 'dashscope', apiKey, baseUrl: url.toString().replace(/\/+$/, ''), model, ...(documentVisionApiKey ? { documentVisionApiKey } : {}) }
}
