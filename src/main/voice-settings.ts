import { app, safeStorage } from 'electron'
import { randomBytes } from 'node:crypto'
import { readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { applyQwenOverlay, qwenOverlayImplicitVoice, qwenOverlayTombstoned, QwenOverlayError, writeQwenOverlayEdit } from './qwen-key-overlay'
import type { QwenRealtimeConfig } from './qwen-realtime'
import { COSYVOICE_DEFAULT_MODEL, supportsCompanionVoice } from '../shared/cosyvoice-catalog'

export type VoiceProvider = 'relay_pcm' | 'aliyun_cosyvoice' | 'qwen_audio_tts' | 'qwen_voice_clone'
export type VoiceConfig = { provider: VoiceProvider; relayUrl: string; apiKey: string; voiceId: string; model?: string; workspaceId?: string; region?: string }
export type VoiceSummary = { configured: boolean; provider: VoiceProvider | 'disabled'; relayUrl?: string; voiceId?: string; model?: string; workspaceId?: string; region?: string }
export type VoiceTrial = { trialId: string; createdAt: string; provider: 'relay_pcm'; relayUrl: string; voiceId: string; firstFrameMs: number; totalMs: number; naturalness: number; childIntelligibility: number; permissionVerified: boolean }
export type VoiceListeningRating = { ratingId: string; candidateId: string; createdAt: string; evaluatorRole: 'parent' | 'child' | 'joint'; naturalness: number; chineseClarity: number; englishMath: number; lowPressureReminder: number; seriousCorrection: number; fatigue30Minutes: number; childAcceptance: number; notes?: string }
export type WakeWordSettings = { childId: string; wakeWord: string; inactivitySeconds: number; enabled: boolean; updatedAt: string }
type StoredVoiceConfig = Omit<VoiceConfig, 'apiKey'> & { encryptedApiKey: string }

export class VoiceSettingsStore {
  private readonly path: string
  private readonly trialsPath: string
  private readonly ratingsPath: string
  private readonly wakeWordsPath: string
  private readonly disabledPath: string
  private wakeWriteQueue: Promise<unknown> = Promise.resolve()
  constructor(userDataPath = app.getPath('userData')) { this.path = join(userDataPath, 'voice-settings.json'); this.trialsPath = join(userDataPath, 'voice-trials.json'); this.ratingsPath = join(userDataPath, 'voice-listening-ratings.json'); this.wakeWordsPath = join(userDataPath, 'companion-wake-words.json'); this.disabledPath = join(userDataPath, 'voice-disabled.json') }
  async load(): Promise<VoiceConfig | null> {
    try {
      if (!safeStorage.isEncryptionAvailable()) return null
      const original = await readFile(this.path).catch(error => { if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') return null; throw error })
      const bytes = applyQwenOverlay(dirname(this.path), 'voice-settings.json', original)
      if (!bytes) return null
      const stored = JSON.parse(bytes.toString('utf8')) as StoredVoiceConfig
      return { provider: stored.provider, relayUrl: stored.relayUrl, voiceId: stored.voiceId, model: stored.model, workspaceId: stored.workspaceId, region: stored.region, apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')) }
    } catch (error) { if (error instanceof QwenOverlayError) throw error; return null }
  }
  async save(config: VoiceConfig): Promise<VoiceConfig> {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('当前系统安全存储不可用，未保存语音服务密钥。')
    const normalized = validateVoiceConfig(config)
    const stored: StoredVoiceConfig = { provider: normalized.provider, relayUrl: normalized.relayUrl, voiceId: normalized.voiceId, model: normalized.model, workspaceId: normalized.workspaceId, region: normalized.region, encryptedApiKey: safeStorage.encryptString(normalized.apiKey).toString('base64') }
    if (writeQwenOverlayEdit(dirname(this.path), 'voice-settings.json', Buffer.from(JSON.stringify(stored)))) {
      await unlink(this.disabledPath).catch((error: NodeJS.ErrnoException) => { if (error.code !== 'ENOENT') throw error })
      return normalized
    }
    const temporary = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try { await writeFile(temporary, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 }); await rename(temporary, this.path) } finally { await unlink(temporary).catch(() => undefined) }
    await unlink(this.disabledPath).catch((error: NodeJS.ErrnoException) => { if (error.code !== 'ENOENT') throw error })
    return normalized
  }
  async clear(): Promise<void> {
    await this.atomicWrite(this.disabledPath, JSON.stringify({ version: 1, disabled: true }))
    if (writeQwenOverlayEdit(dirname(this.path), 'voice-settings.json', null)) return
    await unlink(this.path).catch((error: NodeJS.ErrnoException) => { if (error.code !== 'ENOENT') throw error })
  }
  async loadEffective(realtime: QwenRealtimeConfig | null): Promise<VoiceConfig | null> {
    // A durable explicit disable wins even if a crash interrupted file removal.
    try { await readFile(this.disabledPath); return null } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') return null
    }
    if (qwenOverlayImplicitVoice(dirname(this.path)) && !realtime?.enabled) return null
    if (qwenOverlayTombstoned(dirname(this.path), 'voice-settings.json')) return null
    const configured = await this.load()
    if (configured) { try { return validateVoiceConfig(configured) } catch { return null } }
    // Corrupt or unreadable explicit settings must not silently select another voice.
    try { await readFile(this.path); return null } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') return null
    }
    if (!realtime?.enabled) return null
    let endpoint: URL
    try { endpoint = new URL(realtime.endpoint) } catch { return null }
    const match = endpoint.hostname.match(/^([a-zA-Z0-9-]{2,80})\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com$/)
    if (!match || endpoint.protocol !== 'https:' || endpoint.username || endpoint.password) return null
    return validateVoiceConfig({ provider: 'aliyun_cosyvoice', relayUrl: '', apiKey: realtime.apiKey,
      workspaceId: match[1], region: match[2], model: COSYVOICE_DEFAULT_MODEL,
      voiceId: match[2] === 'cn-beijing' ? 'longanrou_v3' : 'longyingtao_v3' })
  }
  async loadTrials(): Promise<VoiceTrial[]> {
    try {
      const value = JSON.parse(await readFile(this.trialsPath, 'utf8')) as unknown
      return Array.isArray(value) ? value.filter(isVoiceTrial).slice(0, 20) : []
    } catch { return [] }
  }
  async addTrial(trial: VoiceTrial): Promise<VoiceTrial[]> {
    if (!isVoiceTrial(trial)) throw new Error('试听记录格式无效。')
    const trials = [trial, ...(await this.loadTrials())].slice(0, 20)
    await this.atomicWrite(this.trialsPath, JSON.stringify(trials))
    return trials
  }
  async loadListeningRatings(): Promise<VoiceListeningRating[]> {
    try {
      const value = JSON.parse(await readFile(this.ratingsPath, 'utf8')) as unknown
      return Array.isArray(value) ? value.filter(isVoiceListeningRating).slice(0, 100) : []
    } catch { return [] }
  }
  async addListeningRating(rating: VoiceListeningRating): Promise<VoiceListeningRating[]> {
    if (!isVoiceListeningRating(rating)) throw new Error('盲听评分格式无效。')
    const ratings = [rating, ...(await this.loadListeningRatings())].slice(0, 100)
    await this.atomicWrite(this.ratingsPath, JSON.stringify(ratings))
    return ratings
  }
  async loadWakeWord(childId: string): Promise<WakeWordSettings | null> {
    const normalizedChildId = validateChildId(childId)
    try {
      const value = JSON.parse(await readFile(this.wakeWordsPath, 'utf8')) as unknown
      if (!Array.isArray(value)) return null
      const found = value.find((item) => isWakeWordSettings(item) && item.childId === normalizedChildId)
      return found ? validateWakeWordSettings(found) : null
    } catch { return null }
  }
  async saveWakeWord(settings: WakeWordSettings): Promise<WakeWordSettings> {
    const normalized = validateWakeWordSettings(settings)
    const pending = this.wakeWriteQueue.catch(() => undefined).then(async () => {
      let current: WakeWordSettings[] = []
      try {
        const value = JSON.parse(await readFile(this.wakeWordsPath, 'utf8')) as unknown
        if (!Array.isArray(value) || !value.every(isWakeWordSettings)) throw new Error('INVALID_WAKE_SETTINGS')
        current = value.map(validateWakeWordSettings)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new Error('原有唤醒词暂时无法读取，未覆盖原记录。')
      }
      if (normalized.enabled && current.some((item) => item.enabled && item.childId !== normalized.childId && item.wakeWord.toLowerCase() === normalized.wakeWord.toLowerCase())) {
        throw new Error('这个唤醒名字已经有人使用了。请换一个名字，避免进入另一位孩子的学习记录。')
      }
      const next = [normalized, ...current.filter((item) => item.childId !== normalized.childId)].slice(0, 8)
      await this.atomicWrite(this.wakeWordsPath, JSON.stringify(next))
      return normalized
    })
    this.wakeWriteQueue = pending
    return pending
  }
  private async atomicWrite(path: string, content: string): Promise<void> {
    const temporary = `${path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try { await writeFile(temporary, content, { encoding: 'utf8', flag: 'wx', mode: 0o600 }); await rename(temporary, path) } finally { await unlink(temporary).catch(() => undefined) }
  }
}

export function validateWakeWordSettings(settings: WakeWordSettings): WakeWordSettings {
  const childId = validateChildId(settings.childId)
  const wakeWord = settings.wakeWord.trim()
  const compact = wakeWord.replace(/[，。！？、,.!?]/g, '')
  if (compact.length < 2 || compact.length > 12 || /\s/.test(wakeWord) || !/^[a-zA-Z0-9\u3400-\u9fff，。！？、,.!?]+$/.test(wakeWord) || /^\d+$/.test(compact)) {
    throw new Error('唤醒词需为2到12个中文、字母或数字，不能含空格或仅由数字组成。')
  }
  const inactivitySeconds = Math.round(settings.inactivitySeconds)
  if (![30, 45, 60, 90, 120].includes(inactivitySeconds)) throw new Error('静默等待时间无效。')
  if (typeof settings.enabled !== 'boolean') throw new Error('唤醒词启用状态无效。')
  const updatedAt = new Date(settings.updatedAt)
  if (!Number.isFinite(updatedAt.getTime())) throw new Error('唤醒词更新时间无效。')
  return { childId, wakeWord: compact, inactivitySeconds, enabled: settings.enabled, updatedAt: updatedAt.toISOString() }
}

function validateChildId(childId: string): string {
  const normalized = childId.trim()
  if (!/^[a-zA-Z0-9._:-]{2,120}$/.test(normalized)) throw new Error('孩子配置标识无效。')
  return normalized
}

export function summarizeVoiceConfig(config: VoiceConfig | null): VoiceSummary {
  return config ? { configured: true, provider: config.provider, relayUrl: config.relayUrl, voiceId: config.voiceId, model: config.model, workspaceId: config.workspaceId, region: config.region } : { configured: false, provider: 'disabled' }
}

export function validateVoiceConfig(config: VoiceConfig): VoiceConfig {
  if (!['relay_pcm', 'aliyun_cosyvoice', 'qwen_audio_tts', 'qwen_voice_clone'].includes(config.provider)) throw new Error('不支持的自然语音通道。')
  const apiKey = config.apiKey.trim(); const voiceId = config.voiceId.trim()
  if (apiKey.length < 8) throw new Error('语音服务密钥格式不完整。')
  if (!/^[a-zA-Z0-9._:-]{2,120}$/.test(voiceId)) throw new Error('音色 ID 格式无效。')
  if (config.provider === 'aliyun_cosyvoice') {
    const workspaceId = (config.workspaceId ?? '').trim(); const region = (config.region ?? 'cn-beijing').trim().toLowerCase(); const model = (config.model ?? COSYVOICE_DEFAULT_MODEL).trim()
    if (!/^[a-zA-Z0-9-]{2,80}$/.test(workspaceId) || !/^[a-z0-9-]{4,40}$/.test(region) || !/^[a-zA-Z0-9._-]{3,80}$/.test(model)) throw new Error('CosyVoice 工作空间、区域或模型配置无效。')
    if (!supportsCompanionVoice(model, voiceId, region)) throw new Error('CosyVoice 模型、区域与成人女声音色不匹配。')
    return { provider: 'aliyun_cosyvoice', relayUrl: '', apiKey, voiceId, workspaceId, region, model }
  }
  if (config.provider === 'qwen_audio_tts') {
    if (!['longanlingxin', 'longanlufeng'].includes(voiceId)) throw new Error('Qwen-Audio-TTS 只能使用已审核的成人女声音色。')
    const workspaceId = (config.workspaceId ?? '').trim(); const region = (config.region ?? 'cn-beijing').trim().toLowerCase(); const model = (config.model ?? 'qwen-audio-3.0-tts-plus').trim()
    if (!/^[a-zA-Z0-9-]{2,80}$/.test(workspaceId) || !/^[a-z0-9-]{4,40}$/.test(region) || model !== 'qwen-audio-3.0-tts-plus') throw new Error('Qwen-Audio-TTS 工作空间、区域或模型配置无效。')
    return { provider: 'qwen_audio_tts', relayUrl: '', apiKey, voiceId, workspaceId, region, model }
  }
  if (config.provider === 'qwen_voice_clone') {
    const model = (config.model ?? '').trim()
    if (model !== 'qwen3-tts-vc-2026-01-22') throw new Error('参考音色必须使用注册时绑定的 Qwen3-TTS-VC 模型和音色 ID。')
    return { provider: 'qwen_voice_clone', relayUrl: '', apiKey, voiceId, model, region: 'cn-beijing' }
  }
  let url: URL
  try { url = new URL(config.relayUrl.trim()) } catch { throw new Error('语音 relay 地址格式无效。') }
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('语音 relay 必须使用不含账号信息的 HTTPS 地址。')
  return { provider: 'relay_pcm', relayUrl: url.toString(), apiKey, voiceId }
}

function isVoiceTrial(value: unknown): value is VoiceTrial {
  if (!value || typeof value !== 'object') return false
  const trial = value as Partial<VoiceTrial>
  return typeof trial.trialId === 'string' && typeof trial.createdAt === 'string' && trial.provider === 'relay_pcm' && typeof trial.relayUrl === 'string' && typeof trial.voiceId === 'string' && typeof trial.firstFrameMs === 'number' && Number.isFinite(trial.firstFrameMs) && typeof trial.totalMs === 'number' && Number.isFinite(trial.totalMs) && typeof trial.naturalness === 'number' && Number.isInteger(trial.naturalness) && trial.naturalness >= 1 && trial.naturalness <= 5 && typeof trial.childIntelligibility === 'number' && Number.isInteger(trial.childIntelligibility) && trial.childIntelligibility >= 1 && trial.childIntelligibility <= 5 && typeof trial.permissionVerified === 'boolean'
}

function isVoiceListeningRating(value: unknown): value is VoiceListeningRating {
  if (!value || typeof value !== 'object') return false
  const rating = value as Partial<VoiceListeningRating>
  const score = (value: unknown): boolean => typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 5
  return typeof rating.ratingId === 'string' && typeof rating.candidateId === 'string' && /^[a-zA-Z0-9._:-]{2,120}$/.test(rating.candidateId) && typeof rating.createdAt === 'string' && ['parent', 'child', 'joint'].includes(rating.evaluatorRole ?? '') && score(rating.naturalness) && score(rating.chineseClarity) && score(rating.englishMath) && score(rating.lowPressureReminder) && score(rating.seriousCorrection) && score(rating.fatigue30Minutes) && score(rating.childAcceptance) && (rating.notes === undefined || (typeof rating.notes === 'string' && rating.notes.length <= 300))
}

function isWakeWordSettings(value: unknown): value is WakeWordSettings {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<WakeWordSettings>
  return typeof item.childId === 'string' && typeof item.wakeWord === 'string' && typeof item.inactivitySeconds === 'number' && typeof item.enabled === 'boolean' && typeof item.updatedAt === 'string'
}
