import { afterEach, describe, expect, it, vi } from 'vitest'
import { cancelExactLocalSpeech, cancelLocalSpeech, companionPlaybackCaptureGate, speakLocally } from './LocalInputs'
import { getCompanionPlaybackLease, getCompanionVoiceState, speakCompanion, stopCompanionSpeech, type TtsProvider } from './companionVoice'

describe('speech preparation is not playback', () => {
  afterEach(() => { stopCompanionSpeech(); vi.unstubAllGlobals() })
  it('keeps the capture gate open while the managed provider has not delivered audio', async () => {
    vi.stubGlobal('AudioContext', class { currentTime = 0; state = 'running'; destination = {}; close = vi.fn(async () => undefined) })
    let release!: () => void
    const provider: TtsProvider = { id: 'pending-audio', isProductionVoice: true,
      async *stream(request) {
        await new Promise<void>(resolve => { release = resolve; request.signal.addEventListener('abort', () => resolve(), { once: true }) })
        throw new Error('TEST_PROVIDER_ENDED')
      } }
    const speech = speakCompanion('合成尚未到达', provider, undefined, undefined, { managedVoiceRequired: true })
    expect(getCompanionVoiceState()).toBe('preparing')
    expect(companionPlaybackCaptureGate(getCompanionVoiceState(), 10000, 0)).toEqual({ blocked: false, blockedUntil: 0 })
    stopCompanionSpeech(); release(); await expect(speech).resolves.toBe('text_only')
  })
  it('keeps capture open while the offline voice waits for its actual start event', async () => {
    let utterance!: SpeechSynthesisUtterance
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(),
      getVoices: () => [{ name: 'Local', lang: 'zh-CN', localService: true }],
      speak: vi.fn(value => { utterance = value }) })
    await speakCompanion('本机提示', null, undefined, undefined, { localOnly: true })
    expect(getCompanionVoiceState()).toBe('preparing')
    expect(companionPlaybackCaptureGate(getCompanionVoiceState(), 10000, 0).blocked).toBe(false)
    utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
    expect(getCompanionVoiceState()).toBe('speaking')
    expect(companionPlaybackCaptureGate(getCompanionVoiceState(), 10000, 0).blocked).toBe(true)
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
  })

  it('cannot resurrect cancelled offline preparation through a late start callback', async () => {
    let utterance!: SpeechSynthesisUtterance
    const started = vi.fn(), ended = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [{ name: 'Local', lang: 'zh-CN', localService: true }],
      speak: vi.fn(value => { utterance = value }) })
    await speakCompanion('旧提示', null, undefined, started, { localOnly: true, onPlaybackEnd: ended })
    stopCompanionSpeech()
    utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
    expect(getCompanionVoiceState()).not.toBe('speaking')
    expect(started).not.toHaveBeenCalled(); expect(ended).not.toHaveBeenCalled()
  })

  it('can explicitly interrupt an owned offline utterance already queued by Windows before its start callback', () => {
    let utterance!: SpeechSynthesisUtterance
    const cancel = vi.fn(), started = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel, getVoices: () => [{ name: 'Local', lang: 'zh-CN', localService: true }],
      speak: vi.fn(value => { utterance = value }) })
    try {
      speakLocally('合成提示', 'current-session', undefined, started, { localOnly: true })
      expect(utterance).toBeDefined()
      expect(getCompanionPlaybackLease()).toBeNull()
      const pending = getCompanionPlaybackLease(true)!
      expect(pending).toMatchObject({ sessionId: 'current-session', kind: 'local_sapi' })
      cancel.mockClear()
      expect(cancelExactLocalSpeech(pending.id, pending.generation + 1, true)).toBe(false)
      expect(cancelExactLocalSpeech(pending.id, pending.generation, true)).toBe(true)
      expect(cancel).toHaveBeenCalledTimes(1)
      utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
      expect(started).not.toHaveBeenCalled()
    } finally {
      cancelLocalSpeech()
    }
  })
})
