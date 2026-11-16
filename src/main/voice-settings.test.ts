import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  encryptionAvailable: true,
  encryptString: vi.fn((value: string) => Buffer.from(`protected:${value}`, 'utf8')),
  decryptString: vi.fn((value: Buffer) => {
    const decoded = value.toString('utf8')
    if (!decoded.startsWith('protected:')) throw new Error('invalid protected value')
    return decoded.slice('protected:'.length)
  }),
}))

vi.mock('electron', () => ({
  app: { getPath: () => '' },
  safeStorage: {
    isEncryptionAvailable: () => electron.encryptionAvailable,
    encryptString: electron.encryptString,
    decryptString: electron.decryptString,
  },
}))

import { VoiceSettingsStore, summarizeVoiceConfig, validateVoiceConfig, validateWakeWordSettings, type VoiceConfig } from './voice-settings'
import { defaultQwenRealtimeConfig } from './qwen-realtime'

const config: VoiceConfig = {
  provider: 'relay_pcm', relayUrl: 'https://voice.example/pcm', voiceId: 'original-warm-female-v1', apiKey: 'synthetic-a5aeed401d4920d03b18',
}

describe('VoiceSettingsStore', () => {
  let directory = ''
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'hsc-voice-settings-')); electron.encryptionAvailable = true })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

  it('provides recovery speech only from the enabled official workspace and preserves explicit voices', async () => {
    const store = new VoiceSettingsStore(directory)
    const realtime = defaultQwenRealtimeConfig('synthetic-key', 'https://ws-synthetic.cn-beijing.maas.aliyuncs.com')
    expect(await store.loadEffective(null)).toBeNull()
    expect(await store.loadEffective({ ...realtime, enabled: false })).toBeNull()
    expect(await store.loadEffective({ ...realtime, endpoint: 'https://untrusted.example' })).toBeNull()
    expect(await store.loadEffective(realtime)).toMatchObject({ provider: 'aliyun_cosyvoice', model: 'cosyvoice-v3-flash', voiceId: 'longanrou_v3', workspaceId: 'ws-synthetic', apiKey: 'synthetic-key' })
    expect(await store.loadEffective({ ...realtime, endpoint: 'https://ws-synthetic.ap-southeast-1.maas.aliyuncs.com' })).toMatchObject({ voiceId: 'longyingtao_v3' })
    expect(await readdir(directory)).toEqual([])
    await store.save(config)
    expect(await store.loadEffective(realtime)).toMatchObject(config)
    await writeFile(join(directory, 'voice-settings.json'), 'invalid')
    expect(await store.loadEffective(realtime)).toBeNull()
  })

  it('persists explicit speech disable across restart without overriding it with realtime credentials', async () => {
    const realtime = defaultQwenRealtimeConfig('synthetic-key', 'https://ws-synthetic.cn-beijing.maas.aliyuncs.com')
    const store = new VoiceSettingsStore(directory)
    await store.clear()
    expect(await new VoiceSettingsStore(directory).loadEffective(realtime)).toBeNull()
    await store.save(config)
    expect(await new VoiceSettingsStore(directory).loadEffective(realtime)).toMatchObject(config)
  })

  it('stores only encrypted credentials and never returns a key in the parent-facing summary', async () => {
    const store = new VoiceSettingsStore(directory)
    await store.save(config)
    const raw = await readFile(join(directory, 'voice-settings.json'), 'utf8')
    expect(raw).not.toContain(config.apiKey)
    expect(await store.load()).toEqual({ ...config, relayUrl: 'https://voice.example/pcm' })
    expect(summarizeVoiceConfig(await store.load())).toEqual({ configured: true, provider: 'relay_pcm', relayUrl: config.relayUrl, voiceId: config.voiceId })
  })

  it('refuses plaintext-unsafe and malformed relay configuration', () => {
    expect(() => validateVoiceConfig({ ...config, relayUrl: 'http://voice.example/pcm' })).toThrow('HTTPS')
    expect(() => validateVoiceConfig({ ...config, relayUrl: 'https://key@voice.example/pcm' })).toThrow('HTTPS')
    expect(() => validateVoiceConfig({ ...config, voiceId: '模仿妈妈' })).toThrow('音色 ID')
  })

  it('accepts only a model-bound Qwen reference voice ID', () => {
    const cloned: VoiceConfig = { provider: 'qwen_voice_clone', relayUrl: '', apiKey: 'synthetic-a5aeed401d4920d03b18', voiceId: 'qwen3-tts-vc-hsc-reference-1234', model: 'qwen3-tts-vc-2026-01-22', region: 'cn-beijing' }
    expect(validateVoiceConfig(cloned)).toEqual(cloned)
    expect(() => validateVoiceConfig({ ...cloned, model: 'qwen3-tts-flash' })).toThrow('注册时绑定')
    expect(() => validateVoiceConfig({ ...cloned, voiceId: '无效 音色' })).toThrow('音色 ID')
  })

  it('rejects CosyVoice model and regional mismatches before sending synthesis', () => {
    const cosy: VoiceConfig = { provider: 'aliyun_cosyvoice', relayUrl: '', apiKey: 'synthetic-key', voiceId: 'longanrou_v3', workspaceId: 'ws-synthetic', region: 'cn-beijing', model: 'cosyvoice-v3-flash' }
    expect(validateVoiceConfig(cosy)).toEqual(cosy)
    expect(() => validateVoiceConfig({ ...cosy, model: 'cosyvoice-v3-plus' })).toThrow('不匹配')
    expect(() => validateVoiceConfig({ ...cosy, region: 'ap-southeast-1' })).toThrow('不匹配')
    expect(validateVoiceConfig({ ...cosy, model: 'cosyvoice-v3-plus', voiceId: 'longanhuan' })).toMatchObject({ voiceId: 'longanhuan' })
    expect(() => validateVoiceConfig({ ...cosy, model: 'arbitrary-model' })).toThrow('不匹配')
  })

  it('does not replace existing protected settings if OS secure storage is unavailable', async () => {
    const path = join(directory, 'voice-settings.json')
    await writeFile(path, 'previous-protected-value')
    electron.encryptionAvailable = false
    await expect(new VoiceSettingsStore(directory).save(config)).rejects.toThrow('安全存储不可用')
    expect(await readFile(path, 'utf8')).toBe('previous-protected-value')
  })

  it('deletes the local credential idempotently', async () => {
    const store = new VoiceSettingsStore(directory)
    await store.save(config)
    await store.clear(); await store.clear()
    expect(await readdir(directory)).toEqual(['voice-disabled.json'])
    expect(await store.load()).toBeNull()
  })

  it('persists only fixed-text candidate metrics and bounded parent scores across restart', async () => {
    const store = new VoiceSettingsStore(directory)
    const trials = await store.addTrial({ trialId: 'trial-1', createdAt: '2026-08-27T00:00:00.000Z', provider: 'relay_pcm', relayUrl: config.relayUrl, voiceId: config.voiceId, firstFrameMs: 280, totalMs: 1320, naturalness: 4, childIntelligibility: 5, permissionVerified: true })
    expect(trials).toHaveLength(1)
    expect(await new VoiceSettingsStore(directory).loadTrials()).toEqual(trials)
    await expect(store.addTrial({ ...trials[0], trialId: 'bad-score', naturalness: 6 })).rejects.toThrow('试听记录')
  })

  it('persists the full blind-listening rubric without audio, transcript, child ID, or credential', async () => {
    const store = new VoiceSettingsStore(directory)
    const ratings = await store.addListeningRating({ ratingId: 'rating-1', candidateId: 'longyingtao_v3', createdAt: '2026-08-27T00:00:00.000Z', evaluatorRole: 'joint', naturalness: 4, chineseClarity: 5, englishMath: 4, lowPressureReminder: 5, seriousCorrection: 4, fatigue30Minutes: 4, childAcceptance: 5, notes: '固定文本盲听' })
    expect(ratings).toHaveLength(1)
    const raw = await readFile(join(directory, 'voice-listening-ratings.json'), 'utf8')
    expect(raw).not.toContain(config.apiKey)
    expect(await new VoiceSettingsStore(directory).loadListeningRatings()).toEqual(ratings)
    await expect(store.addListeningRating({ ...ratings[0], ratingId: 'bad-rating', englishMath: 0 })).rejects.toThrow('盲听评分')
  })

  it('persists separate wake words per child across restart', async () => {
    const store = new VoiceSettingsStore(directory)
    const saved = await store.saveWakeWord({ childId: 'child-primary-4', wakeWord: ' 小知。 ', inactivitySeconds: 60, enabled: true, updatedAt: '2026-08-30T00:00:00Z' })
    await store.saveWakeWord({ childId: 'child-junior-3', wakeWord: '小伴', inactivitySeconds: 90, enabled: true, updatedAt: '2026-08-30T00:01:00Z' })
    expect(saved.wakeWord).toBe('小知')
    expect(await new VoiceSettingsStore(directory).loadWakeWord('child-primary-4')).toEqual(saved)
    expect((await new VoiceSettingsStore(directory).loadWakeWord('child-junior-3'))?.wakeWord).toBe('小伴')
  })

  it('keeps the previous wake word after a rejected rename and store restart', async () => {
    const store = new VoiceSettingsStore(directory)
    const base = { inactivitySeconds: 60, enabled: true, updatedAt: '2026-08-30T00:00:00Z' }
    const original = await store.saveWakeWord({ ...base, childId: 'child-a', wakeWord: '小知' })
    await store.saveWakeWord({ ...base, childId: 'child-b', wakeWord: '小伴' })
    await expect(store.saveWakeWord({ ...base, childId: 'child-a', wakeWord: '小伴' })).rejects.toThrow('已经有人使用')
    const restarted = new VoiceSettingsStore(directory)
    expect(await restarted.loadWakeWord('child-a')).toEqual(original)
    expect((await restarted.loadWakeWord('child-b'))?.wakeWord).toBe('小伴')
  })

  it('rejects wake words likely to be accidental or impossible to match', () => {
    const base = { childId: 'child-primary-4', inactivitySeconds: 60, enabled: true, updatedAt: '2026-08-30T00:00:00Z' }
    expect(() => validateWakeWordSettings({ ...base, wakeWord: '好' })).toThrow('2到12个')
    expect(() => validateWakeWordSettings({ ...base, wakeWord: '小 知' })).toThrow('不能含空格')
    expect(() => validateWakeWordSettings({ ...base, wakeWord: '1234' })).toThrow('仅由数字')
    expect(() => validateWakeWordSettings({ ...base, wakeWord: '小知', inactivitySeconds: 10 })).toThrow('静默等待')
  })

  it('serializes simultaneous child setup and rejects ambiguous shared wake words', async () => {
    const store = new VoiceSettingsStore(directory)
    const base = { inactivitySeconds: 60, enabled: true, updatedAt: '2026-09-05T00:00:00Z' }
    await Promise.all([
      store.saveWakeWord({ ...base, childId: 'child-a', wakeWord: '小知' }),
      store.saveWakeWord({ ...base, childId: 'child-b', wakeWord: '小伴' }),
    ])
    expect((await store.loadWakeWord('child-a'))?.wakeWord).toBe('小知')
    await expect(store.saveWakeWord({ ...base, childId: 'child-b', wakeWord: '小知' })).rejects.toThrow('已经有人使用')
    expect((await new VoiceSettingsStore(directory).loadWakeWord('child-b'))?.wakeWord).toBe('小伴')
  })

  it('does not replace damaged wake settings with a new child record', async () => {
    const path = join(directory, 'companion-wake-words.json')
    await writeFile(path, 'damaged', 'utf8')
    await expect(new VoiceSettingsStore(directory).saveWakeWord({ childId: 'child-a', wakeWord: '小知', inactivitySeconds: 60, enabled: true, updatedAt: '2026-09-05T00:00:00Z' })).rejects.toThrow('未覆盖')
    expect(await readFile(path, 'utf8')).toBe('damaged')
  })
})
