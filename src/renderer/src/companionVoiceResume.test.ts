import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getCompanionVoiceState, speakCompanion, stopCompanionSpeech, type TtsProvider } from './companionVoice'

describe('owned audio context preparation', () => {
  let resume!: () => void
  let close: ReturnType<typeof vi.fn>, stream: ReturnType<typeof vi.fn<TtsProvider['stream']>>
  let provider: TtsProvider
  beforeEach(() => {
    vi.useFakeTimers()
    close = vi.fn(async () => undefined)
    vi.stubGlobal('AudioContext', class {
      state = 'suspended'; currentTime = 0; close = close
      resume = () => new Promise<void>(resolve => { resume = () => { this.state = 'running'; resolve() } })
    })
    stream = vi.fn(async function* () { throw new Error('PROVIDER_MUST_NOT_START') })
    provider = { id: 'blocked-audio', isProductionVoice: true, stream }
  })
  afterEach(() => { stopCompanionSpeech(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })
  const flush = async (): Promise<void> => { for (let index = 0; index < 12; index++) await Promise.resolve() }

  it('settles a cancelled resume without waiting for the browser promise', async () => {
    const settled = vi.fn(), ended = vi.fn()
    const pending = speakCompanion('旧播放', provider, undefined, undefined,
      { managedVoiceRequired: true, onPlaybackEnd: ended }).then(settled)
    await flush(); stopCompanionSpeech(); await flush()
    expect(settled).toHaveBeenCalledExactlyOnceWith('text_only')
    expect(stream).not.toHaveBeenCalled(); expect(ended).not.toHaveBeenCalled()
    resume(); await pending
  })

  it('refuses a suspended context after the preparation deadline', async () => {
    const settled = vi.fn(), unavailable = vi.fn()
    const pending = speakCompanion('播放受阻', provider, undefined, undefined,
      { managedVoiceRequired: true, onNotDelivered: unavailable }).then(settled)
    await vi.advanceTimersByTimeAsync(1500)
    expect(settled).toHaveBeenCalledExactlyOnceWith('text_only')
    expect(unavailable).toHaveBeenCalledExactlyOnceWith('unavailable')
    expect(close).toHaveBeenCalledOnce(); expect(stream).not.toHaveBeenCalled()
    resume(); await pending; expect(getCompanionVoiceState()).toBe('error')
  })

  it('does not let a late cancelled resume reset the next preparing owner', async () => {
    const oldSettled = vi.fn()
    const old = speakCompanion('旧播放', provider, undefined, undefined,
      { managedVoiceRequired: true }).then(oldSettled)
    await flush(); const oldResume = resume
    const next = speakCompanion('新播放', provider, undefined, undefined, { managedVoiceRequired: true })
    await flush(); oldResume(); await flush()
    expect(oldSettled).toHaveBeenCalledExactlyOnceWith('text_only')
    expect(getCompanionVoiceState()).toBe('preparing')
    expect(stream).not.toHaveBeenCalled()
    stopCompanionSpeech(); resume(); await Promise.all([old, next])
  })

  it('rejects a late browser resume even when the deadline timer has been delayed', async () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(10)
    const pending = speakCompanion('过期准备', provider, undefined, undefined, { managedVoiceRequired: true })
    await flush(); now.mockReturnValue(1511); resume()
    await expect(pending).resolves.toBe('text_only')
    expect(stream).not.toHaveBeenCalled(); expect(close).toHaveBeenCalledOnce()
  })

  it('does not schedule a buffered chunk after cancellation releases backpressure', async () => {
    const sources: ReturnType<typeof vi.fn>[] = []
    vi.stubGlobal('AudioContext', class {
      state = 'running'; currentTime = 0; destination = {}; close = close
      createBuffer() { return { duration: 10, getChannelData: () => new Float32Array(2) } }
      createBufferSource() {
        const start = vi.fn(); sources.push(start)
        return Object.assign(new EventTarget(), { start, stop: vi.fn(), connect: vi.fn() })
      }
    })
    const ended = vi.fn()
    const buffered: TtsProvider = { id: 'backpressure', isProductionVoice: true,
      async *stream() { for (let i = 0; i < 2; i++) yield { pcm16: new Int16Array([1, 2]).buffer, sampleRate: 16000 } } }
    const pending = speakCompanion('缓冲中的旧音频', buffered, undefined, undefined,
      { managedVoiceRequired: true, onPlaybackEnd: ended })
    await flush(); expect(sources).toHaveLength(1)
    stopCompanionSpeech(); await expect(pending).resolves.toBe('text_only')
    expect(sources).toHaveLength(1); expect(ended).not.toHaveBeenCalled()
  })
})
