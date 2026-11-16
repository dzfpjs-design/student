import { denyUnpricedMoneyEgress } from './money-egress'
import { app, safeStorage } from 'electron'
import { execFile } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { applyQwenOverlay, qwenOverlayTombstoned, QwenOverlayError, writeQwenOverlayEdit } from './qwen-key-overlay'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

export const QWEN_REALTIME_MODEL = 'qwen3.8-omni-flash-realtime'
export const QWEN_REALTIME_DEFAULT_VOICE = 'Tina'

export type QwenRealtimeConfig = {
  apiKey: string
  endpoint: string
  model: typeof QWEN_REALTIME_MODEL
  voice: string
  enabled: boolean
}

export type QwenRealtimeSummary = Omit<QwenRealtimeConfig, 'apiKey'> & { configured: boolean }
type StoredQwenRealtimeConfig = Omit<QwenRealtimeConfig, 'apiKey'> & { encryptedApiKey: string }

export class QwenRealtimeSettingsStore {
  private readonly path: string

  constructor(userDataPath = app.getPath('userData')) {
    this.path = join(userDataPath, 'qwen-realtime-settings.json')
  }

  async load(): Promise<QwenRealtimeConfig | null> {
    try {
      if (!safeStorage.isEncryptionAvailable()) return null
      const original = await readFile(this.path).catch(error => { if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') return null; throw error })
      const bytes = applyQwenOverlay(dirname(this.path), 'qwen-realtime-settings.json', original)
      if (!bytes) return null
      const stored = JSON.parse(bytes.toString('utf8')) as StoredQwenRealtimeConfig
      return validateQwenRealtimeConfig({
        apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')),
        endpoint: stored.endpoint,
        model: stored.model,
        voice: stored.voice,
        enabled: stored.enabled,
      })
    } catch (error) {
      if (error instanceof QwenOverlayError) throw error
      return null
    }
  }

  async save(config: QwenRealtimeConfig): Promise<QwenRealtimeConfig> {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('当前系统安全存储不可用，未保存实时语音配置。')
    const normalized = validateQwenRealtimeConfig(config)
    const stored: StoredQwenRealtimeConfig = {
      endpoint: normalized.endpoint,
      model: normalized.model,
      voice: normalized.voice,
      enabled: normalized.enabled,
      encryptedApiKey: safeStorage.encryptString(normalized.apiKey).toString('base64'),
    }
    if (writeQwenOverlayEdit(dirname(this.path), 'qwen-realtime-settings.json', Buffer.from(JSON.stringify(stored)))) return normalized
    const temporary = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try {
      await writeFile(temporary, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
      await rename(temporary, this.path)
    } finally {
      await unlink(temporary).catch(() => undefined)
    }
    return normalized
  }

  async loadOrImport(options: { keychainService?: string; environmentKey?: string; endpoint?: string }): Promise<QwenRealtimeConfig | null> {
    const existing = await this.load()
    if (existing) return existing
    if (qwenOverlayTombstoned(dirname(this.path), 'qwen-realtime-settings.json')) return null
    const endpoint = options.endpoint?.trim()
    // Qwen 3.8 Realtime rejects the generic DashScope host. Do not persist a
    // credential into a configuration that can never establish signaling.
    if (!endpoint) return null
    const environmentKey = options.environmentKey?.trim()
    if (environmentKey) return this.save(defaultQwenRealtimeConfig(environmentKey, endpoint))
    const service = options.keychainService?.trim()
    if (process.platform !== 'darwin' || !service || !safeStorage.isEncryptionAvailable()) return null
    try {
      const { stdout } = await execFileAsync('/usr/bin/security', ['find-generic-password', '-s', service, '-w'], {
        encoding: 'utf8', maxBuffer: 8_192,
      })
      return this.save(defaultQwenRealtimeConfig(stdout.trim(), endpoint))
    } catch {
      return null
    }
  }

  async resolveApiKey(candidate: string | undefined, options: { keychainService?: string; environmentKey?: string }): Promise<string> {
    const direct = candidate?.trim()
    if (direct) return direct
    if (qwenOverlayTombstoned(dirname(this.path), 'qwen-realtime-settings.json')) return ''
    const existing = await this.load()
    if (existing?.apiKey) return existing.apiKey
    const environmentKey = options.environmentKey?.trim()
    if (environmentKey) return environmentKey
    const service = options.keychainService?.trim()
    if (process.platform !== 'darwin' || !service) return ''
    try {
      const { stdout } = await execFileAsync('/usr/bin/security', ['find-generic-password', '-s', service, '-w'], {
        encoding: 'utf8', maxBuffer: 8_192,
      })
      return stdout.trim()
    } catch {
      return ''
    }
  }

  async clear(): Promise<void> {
    if (writeQwenOverlayEdit(dirname(this.path), 'qwen-realtime-settings.json', null)) return
    await unlink(this.path).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== 'ENOENT') throw error
    })
  }
}

export function defaultQwenRealtimeConfig(apiKey: string, endpoint: string): QwenRealtimeConfig {
  return {
    apiKey,
    endpoint,
    model: QWEN_REALTIME_MODEL,
    voice: QWEN_REALTIME_DEFAULT_VOICE,
    enabled: true,
  }
}

export function validateQwenRealtimeConfig(config: QwenRealtimeConfig): QwenRealtimeConfig {
  const apiKey = config.apiKey.trim()
  if (apiKey.length < 16) throw new Error('实时语音 API Key 格式不完整。')
  if (config.model !== QWEN_REALTIME_MODEL) throw new Error('实时语音模型必须为 Qwen3.8 Omni Flash Realtime。')
  let endpoint: URL
  try { endpoint = new URL(config.endpoint.trim()) } catch { throw new Error('实时语音接入地址无效。') }
  if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password
    || !/^[a-zA-Z0-9_-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com$/.test(endpoint.hostname)) {
    throw new Error('实时语音必须使用包含真实 Workspace ID 的阿里云百炼 HTTPS 地址。')
  }
  const voice = config.voice.trim()
  if (!/^[a-zA-Z0-9._:-]{2,180}$/.test(voice)) throw new Error('实时语音音色 ID 无效。')
  if (typeof config.enabled !== 'boolean') throw new Error('实时语音启用状态无效。')
  return { apiKey, endpoint: endpoint.origin, model: QWEN_REALTIME_MODEL, voice, enabled: config.enabled }
}

export function summarizeQwenRealtime(config: QwenRealtimeConfig | null): QwenRealtimeSummary {
  return config ? {
    configured: true,
    endpoint: config.endpoint,
    model: config.model,
    voice: config.voice,
    enabled: config.enabled,
  } : {
    configured: false,
    endpoint: '',
    model: QWEN_REALTIME_MODEL,
    voice: QWEN_REALTIME_DEFAULT_VOICE,
    enabled: false,
  }
}

export async function exchangeQwenRealtimeSdp(
  config: QwenRealtimeConfig,
  offerSdp: string,
  fetchImpl: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<{ answerSdp: string; model: string; voice: string }> {
  signal?.throwIfAborted()
  denyUnpricedMoneyEgress('automatic_rtc_sdp')
  const normalized = validateQwenRealtimeConfig(config)
  if (!normalized.enabled) throw new Error('实时语音已停用。')
  if (typeof offerSdp !== 'string' || offerSdp.length < 64 || offerSdp.length > 512_000
    || !offerSdp.startsWith('v=0') || !offerSdp.includes('m=audio')) {
    throw new Error('WebRTC Offer SDP 无效。')
  }
  const url = new URL('/api/v1/webrtc/realtime', normalized.endpoint)
  url.searchParams.set('model', normalized.model)
  const requestSignal = signal ? AbortSignal.any([signal, AbortSignal.timeout(20_000)]) : AbortSignal.timeout(20_000)
  requestSignal.throwIfAborted()
  const response = await fetchImpl(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${normalized.apiKey}`, 'Content-Type': 'application/sdp' },
    body: offerSdp,
    redirect: 'error',
    signal: requestSignal,
  })
  requestSignal.throwIfAborted()
  const answerSdp = await response.text()
  requestSignal.throwIfAborted()
  if (!response.ok) throw new Error(`Qwen Realtime 信令失败（HTTP ${response.status}）。`)
  if (answerSdp.length < 64 || answerSdp.length > 512_000 || !answerSdp.startsWith('v=0') || !answerSdp.includes('m=audio')) {
    throw new Error('Qwen Realtime 没有返回有效 Answer SDP。')
  }
  return { answerSdp, model: normalized.model, voice: normalized.voice }
}

export async function testQwenRealtimeConfig(
  config: QwenRealtimeConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<{ ok: true; latencyMs: number; model: string }> {
  denyUnpricedMoneyEgress('realtime_config_test')
  const normalized = validateQwenRealtimeConfig(config)
  const started = Date.now()
  const url = new URL('/api/v1/models', normalized.endpoint)
  const response = await fetchImpl(url, {
    headers: { Authorization: `Bearer ${normalized.apiKey}` },
    redirect: 'error',
    signal: AbortSignal.timeout(15_000),
  })
  if (!response.ok) throw new Error(`Qwen Realtime 配置验证失败（HTTP ${response.status}）。`)
  const body = await response.json() as {
    data?: Array<{ id?: string }>
    output?: { models?: Array<{ model?: string }> }
  }
  const availableModels = [
    ...(body.data ?? []).map((item) => item.id),
    ...(body.output?.models ?? []).map((item) => item.model),
  ]
  if (!availableModels.includes(normalized.model)) {
    throw new Error('当前工作空间没有返回 qwen3.8-omni-flash-realtime。')
  }
  return { ok: true, latencyMs: Date.now() - started, model: normalized.model }
}
