import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getCompanionPlaybackLocalOnly, getCompanionVoiceState, isCacheableFixedTtsText, LocalAgentStreamingTtsProvider, PcmPlaybackCache, speakCompanion, stopCompanionSpeech, subscribeCompanionVoice, type TtsProvider } from './companionVoice'
import { resetRuntimeCache } from './api'

describe('local-only speech privacy', () => {
  const privateReply = '你让我只在本机记住：我怕同学嘲笑，不要告诉别人。'
  const localVoice = { name: 'Local Chinese', voiceURI: 'local-zh', lang: 'zh-CN', localService: true } as SpeechSynthesisVoice
  const remoteVoice = { name: 'Microsoft Xiaoxiao', voiceURI: 'remote-zh', lang: 'zh-CN', localService: false } as SpeechSynthesisVoice
  let voices: SpeechSynthesisVoice[]
  let speak: ReturnType<typeof vi.fn>
  let stream: ReturnType<typeof vi.fn<TtsProvider['stream']>>
  let provider: TtsProvider

  beforeEach(() => {
    voices = [remoteVoice, localVoice]
    speak = vi.fn()
    stream = vi.fn(async function* () { throw new Error('MUST_NOT_CONTACT_MANAGED_PROVIDER') })
    provider = { id: 'cloud-backed', isProductionVoice: true, stream }
    vi.stubGlobal('AudioContext', class { currentTime = 0; close = vi.fn().mockResolvedValue(undefined) })
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => voices })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  })

  afterEach(() => {
    stopCompanionSpeech()
    vi.unstubAllGlobals()
  })

  it.each([
    { localOnly: true },
    { localOnly: true, sourceAgentTurnId: 'private-memory-turn' },
  ])('bypasses the managed provider and selects only a verified local voice: %j', async (privacy) => {
    const onPlaybackStart = vi.fn()
    await expect(speakCompanion(privateReply, provider, new PcmPlaybackCache(), onPlaybackStart, privacy)).resolves.toBe('system_offline')
    expect(getCompanionPlaybackLocalOnly()).toBe(true)
    expect(stream).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ text: privateReply, voice: localVoice }))
    const utterance = speak.mock.calls[0][0] as SpeechSynthesisUtterance
    utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
    expect(onPlaybackStart).toHaveBeenCalledOnce()
    utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
    expect(getCompanionVoiceState()).toBe('idle')
  })

  it.each(['remote_only', 'unavailable', 'unknown_locality'])('keeps text without cloud/default-voice fallback when voices are %s', async (mode) => {
    voices = mode === 'remote_only' ? [remoteVoice] : mode === 'unknown_locality'
      ? [{ name: 'Unknown', voiceURI: 'unknown', lang: 'zh-CN' } as SpeechSynthesisVoice] : []
    const onPlaybackStart = vi.fn()
    await expect(speakCompanion(privateReply, provider, undefined, onPlaybackStart, { localOnly: true })).resolves.toBe('text_only')
    expect(stream).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
    expect(onPlaybackStart).not.toHaveBeenCalled()
    expect(getCompanionVoiceState()).toBe('error')
  })

  it('returns text-only if local synthesis throws, without retrying a cloud provider', async () => {
    speak.mockImplementation(() => { throw new Error('LOCAL_SYNTHESIS_FAILED') })
    await expect(speakCompanion(privateReply, provider, undefined, undefined, { localOnly: true })).resolves.toBe('text_only')
    expect(stream).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(getCompanionVoiceState()).toBe('error')
  })

  it('never changes a learning reply into a system voice when managed cloud speech fails', async () => {
    const notDelivered = vi.fn()
    await expect(speakCompanion('请讲解这道题', provider, undefined, undefined, {
      managedVoiceRequired: true,
      onNotDelivered: notDelivered,
    })).resolves.toBe('text_only')
    expect(stream).toHaveBeenCalledOnce()
    expect(speak).not.toHaveBeenCalled()
    expect(notDelivered).toHaveBeenCalledExactlyOnceWith('unavailable')
    expect(getCompanionVoiceState()).toBe('error')
  })
  it('reports OS audio completion only on the current utterance end, never on delivery or cancellation', async () => {
    const done = vi.fn()
    await speakCompanion(privateReply, provider, undefined, undefined, { localOnly: true, onPlaybackEnd: done })
    expect(done).not.toHaveBeenCalled()
    const first = speak.mock.calls[0][0] as SpeechSynthesisUtterance
    first.onend?.(new Event('end') as SpeechSynthesisEvent)
    expect(done).toHaveBeenCalledOnce()
    await speakCompanion('第二段', provider, undefined, undefined, { localOnly: true, onPlaybackEnd: done })
    const second = speak.mock.calls[1][0] as SpeechSynthesisUtterance
    stopCompanionSpeech()
    second.onend?.(new Event('end') as SpeechSynthesisEvent)
    expect(done).toHaveBeenCalledOnce()
  })

  it('waits for Windows to enumerate local voices before the first private prompt', async () => {
    voices = []
    const events = new EventTarget()
    Object.assign(window.speechSynthesis, { addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events) })
    const result = speakCompanion(privateReply, provider, undefined, undefined, { localOnly: true })
    expect(speak).not.toHaveBeenCalled()
    voices = [localVoice]
    events.dispatchEvent(new Event('voiceschanged'))
    await expect(result).resolves.toBe('system_offline')
    expect(speak).toHaveBeenCalledOnce()
    expect(stream).not.toHaveBeenCalled()
  })

  it('does not resurrect a cancelled prompt when voices finish loading', async () => {
    voices = []
    const events = new EventTarget()
    Object.assign(window.speechSynthesis, { addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events) })
    const result = speakCompanion(privateReply, provider, undefined, undefined, { localOnly: true })
    stopCompanionSpeech()
    voices = [localVoice]
    events.dispatchEvent(new Event('voiceschanged'))
    await expect(result).resolves.toBe('text_only')
    expect(speak).not.toHaveBeenCalled()
  })

  it('bounds voice enumeration without allowing a remote default for private text', async () => {
    vi.useFakeTimers()
    try {
      voices = []
      const events = new EventTarget()
      Object.assign(window.speechSynthesis, { addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events) })
      const result = speakCompanion(privateReply, provider, undefined, undefined, { localOnly: true })
      await vi.advanceTimersByTimeAsync(1600)
      await expect(result).resolves.toBe('text_only')
      expect(speak).not.toHaveBeenCalled()
      expect(stream).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('blocks direct local-only provider use before any HTTP request', async () => {
    const localOnlyProvider = new LocalAgentStreamingTtsProvider('session-private', { localOnly: true, sourceAgentTurnId: 'memory-private' })
    await expect((async () => {
      for await (const _chunk of localOnlyProvider.stream({ text: privateReply, requestId: 'private-action', signal: new AbortController().signal })) { /* No chunks may be requested. */ }
    })()).rejects.toThrow('LOCAL_ONLY_SYNTHESIS_REQUIRED')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('keeps provider-level local-only restrictions when the caller omits speech options', async () => {
    const privateProvider = new LocalAgentStreamingTtsProvider('session-private', { localOnly: true })
    const requestStream = vi.spyOn(privateProvider, 'stream')
    await expect(speakCompanion(privateReply, privateProvider)).resolves.toBe('system_offline')
    expect(requestStream).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ voice: localVoice }))
  })

  it('sends the originating turn to the local backend for ordinary managed speech', async () => {
    resetRuntimeCache()
    const terminal = new TextEncoder().encode(JSON.stringify({ type: 'generation_completed', request_id: 'ordinary-action', frame_count: 1, pcm_bytes: 2, sample_count: 1 }))
    const data = new Uint8Array(18 + terminal.length)
    const header = new DataView(data.buffer)
    header.setUint32(0, 24_000, true); header.setUint32(4, 2, true)
    header.setUint32(14, terminal.length, true); data.set(terminal, 18)
    vi.mocked(fetch).mockResolvedValue(new Response(data, { status: 200, headers: { 'Content-Type': 'application/x-hsc-pcm16-frame-v2' } }))
    const managed = new LocalAgentStreamingTtsProvider('session-public', { sourceAgentTurnId: 'ordinary-turn' })
    for await (const _chunk of managed.stream({ text: '一般学习指导', requestId: 'ordinary-action', signal: new AbortController().signal })) { /* Read the qualified stream before inspecting its request. */ }
    const request = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/voice/synthesize/stream'))
    expect(request).toBeDefined()
    expect(JSON.parse(request![1]!.body as string)).toMatchObject({
      text: '一般学习指导', local_only: false, source_agent_turn_id: 'ordinary-turn', client_action_id: 'ordinary-action',
    })
    resetRuntimeCache()
  })

  it.each([true, false])('cannot bypass a backend synthesis denial through a remote browser voice (local available: %s)', async (localAvailable) => {
    resetRuntimeCache()
    voices = localAvailable ? [remoteVoice, localVoice] : [remoteVoice]
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 403 }))
    const denied = new LocalAgentStreamingTtsProvider('session-private', { sourceAgentTurnId: 'private-memory-turn' })
    await expect(speakCompanion(privateReply, denied)).resolves.toBe(localAvailable ? 'system_offline' : 'text_only')
    if (localAvailable) expect(speak).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ voice: localVoice }))
    else expect(speak).not.toHaveBeenCalled()
    resetRuntimeCache()
  })
})

describe('companion voice state', () => {
  it('publishes interruption for avatar and barge-in consumers', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeCompanionVoice(listener)
    stopCompanionSpeech()
    expect(['idle', 'interrupted']).toContain(getCompanionVoiceState())
    expect(listener).toHaveBeenCalled()
    unsubscribe()
  })
})

describe('ephemeral TTS cache', () => {
  it('copies bounded PCM chunks and never persists them', () => {
    const cache = new PcmPlaybackCache(1)
    const original = new Int16Array([10, 20]).buffer
    cache.put('fixed-copy', [{ pcm16: original, sampleRate: 16_000 }])
    new Int16Array(original)[0] = 999
    expect(new Int16Array(cache.get('fixed-copy')![0].pcm16)[0]).toBe(10)
    cache.clear()
    expect(cache.get('fixed-copy')).toBeNull()
  })

  it('permits only reviewed fixed prompts to enter the renderer PCM cache', () => {
    expect(isCacheableFixedTtsText('我们继续下一步。')).toBe(true)
    expect(isCacheableFixedTtsText('小明，你今天完成数学了吗？')).toBe(false)
    expect(isCacheableFixedTtsText('自定义短句')).toBe(false)
  })

  it('rejects overlarge PCM cache entries and evicts LRU entries under its byte budget', () => {
    const cache = new PcmPlaybackCache(20)
    cache.put('too-large', [{ pcm16: new ArrayBuffer(600_000), sampleRate: 24_000 }])
    expect(cache.get('too-large')).toBeNull()
    for (let index = 0; index < 5; index += 1) cache.put(`fixed-${index}`, [{ pcm16: new ArrayBuffer(500_000), sampleRate: 24_000 }])
    expect(cache.get('fixed-0')).toBeNull()
    expect(cache.get('fixed-4')).not.toBeNull()
  })
})

describe('managed PCM interruption', () => {
  it('rejects a truncated local-agent PCM stream instead of reporting natural voice success', async () => {
    resetRuntimeCache()
    const incomplete = new Uint8Array(10)
    const view = new DataView(incomplete.buffer)
    view.setUint32(0, 24_000, true)
    view.setUint32(4, 4, true)
    incomplete.set([1, 2], 8)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(incomplete, { status: 200, headers: { 'Content-Type': 'application/x-hsc-pcm16-frame-v2' } })))
    const provider = new LocalAgentStreamingTtsProvider('session-truncated')

    await expect((async () => {
      for await (const _chunk of provider.stream({ text: '测试', requestId: 'truncated', signal: new AbortController().signal })) {
        // The incomplete frame must never be delivered.
      }
    })()).rejects.toThrow('TTS_STREAM_TRUNCATED')
    vi.unstubAllGlobals()
    resetRuntimeCache()
  })

  it('signals the real first scheduled audio frame for synchronized projections', async () => {
    const sources: EventTarget[] = []
    class TinyAudioContext {
      currentTime = 0
      state = 'running'
      destination = {}
      close = vi.fn().mockResolvedValue(undefined)
      createBuffer(_channels: number, length: number, sampleRate: number) {
        const values = new Float32Array(length)
        return { duration: length / sampleRate, getChannelData: () => values }
      }
      createBufferSource() {
        let renderEnd = 0
        const source = Object.assign(new EventTarget(), {
          buffer: null as unknown, connect: vi.fn(), stop: vi.fn(), start: vi.fn(),
        })
        source.start.mockImplementation((when: number) => { renderEnd = when + (source.buffer as AudioBuffer).duration })
        source.addEventListener('ended', () => { this.currentTime = renderEnd }, { once: true })
        sources.push(source)
        return source
      }
    }
    vi.stubGlobal('AudioContext', TinyAudioContext)
    const provider: TtsProvider = {
      id: 'timeline', isProductionVoice: true,
      async *stream() { yield { pcm16: new Int16Array([1, 2]).buffer, sampleRate: 24_000 } }
    }
    const onPlaybackStart = vi.fn()

    const speaking = speakCompanion('同步讲解', provider, undefined, onPlaybackStart)
    expect(getCompanionPlaybackLocalOnly()).toBe(true)
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    expect(getCompanionPlaybackLocalOnly()).toBe(false)
    expect(onPlaybackStart).toHaveBeenCalledOnce()
    sources[0].dispatchEvent(new Event('ended'))
    await expect(speaking).resolves.toBe('managed')
    stopCompanionSpeech()
    expect(getCompanionPlaybackLocalOnly()).toBe(true)
    vi.unstubAllGlobals()
  })

  it('resumes a suspended Windows audio context before claiming managed delivery', async () => {
    const sources: EventTarget[] = []
    const resume = vi.fn()
    class SuspendedAudioContext {
      currentTime = 0
      state: AudioContextState = 'suspended'
      destination = {}
      close = vi.fn().mockResolvedValue(undefined)
      resume = vi.fn(async () => { resume(); this.state = 'running' })
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return { duration: length / sampleRate, getChannelData: () => new Float32Array(length) }
      }
      createBufferSource() {
        let renderEnd = 0
        const source = Object.assign(new EventTarget(), { buffer: null as unknown, connect: vi.fn(), stop: vi.fn(), start: vi.fn() })
        source.start.mockImplementation((when: number) => { renderEnd = when + (source.buffer as AudioBuffer).duration })
        source.addEventListener('ended', () => { this.currentTime = renderEnd }, { once: true })
        sources.push(source)
        return source
      }
    }
    vi.stubGlobal('AudioContext', SuspendedAudioContext)
    const provider: TtsProvider = {
      id: 'windows-suspended', isProductionVoice: true,
      async *stream() { yield { pcm16: new Int16Array([1, 2]).buffer, sampleRate: 24_000 } },
    }
    const speaking = speakCompanion('恢复声音', provider)
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    expect(resume).toHaveBeenCalledOnce()
    sources[0].dispatchEvent(new Event('ended'))
    await expect(speaking).resolves.toBe('managed')
    vi.unstubAllGlobals()
  })

  it('aborts an in-flight stream without letting the older request reset a newer voice state', async () => {
    class QuietAudioContext {
      currentTime = 0
      close = vi.fn().mockResolvedValue(undefined)
    }
    vi.stubGlobal('AudioContext', QuietAudioContext)
    let firstSignal: AbortSignal | undefined
    let finishFirst: (() => void) | undefined
    const first: TtsProvider = {
      id: 'first', isProductionVoice: true,
      async *stream(request) {
        firstSignal = request.signal
        await new Promise<void>((resolve) => { finishFirst = resolve; request.signal.addEventListener('abort', () => resolve(), { once: true }) })
      }
    }
    let secondSignal: AbortSignal | undefined
    const second: TtsProvider = {
      id: 'second', isProductionVoice: true,
      async *stream(request) {
        secondSignal = request.signal
        await new Promise<void>((resolve) => request.signal.addEventListener('abort', () => resolve(), { once: true }))
      }
    }

    const oldRequest = speakCompanion('第一句', first)
    await Promise.resolve()
    const newRequest = speakCompanion('第二句', second)
    await Promise.resolve()
    expect(firstSignal?.aborted).toBe(true)
    finishFirst?.()
    await expect(oldRequest).resolves.toBe('text_only')
    expect(getCompanionVoiceState()).toBe('preparing')
    expect(getCompanionPlaybackLocalOnly()).toBe(true)
    stopCompanionSpeech()
    expect(secondSignal?.aborted).toBe(true)
    await expect(newRequest).resolves.toBe('text_only')
    vi.unstubAllGlobals()
  })
})

describe('offline system-voice disclosure', () => {
  it.each(['never_started', 'never_ended'])('releases the microphone ownership if the OS speech engine %s', async failure => {
    vi.useFakeTimers()
    const end = vi.fn(), unavailable = vi.fn(), speak = vi.fn(), cancel = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(readonly text: string) {} })
    vi.stubGlobal('speechSynthesis', { cancel, speak, getVoices: () => [] })
    try {
      await speakCompanion('测试播报', null, undefined, undefined, { onPlaybackEnd: end, onNotDelivered: unavailable })
      const utterance = speak.mock.calls[0][0] as SpeechSynthesisUtterance
      if (failure === 'never_ended') utterance.onstart?.(new Event('start') as SpeechSynthesisEvent)
      await vi.advanceTimersByTimeAsync(20_000)
      expect(getCompanionVoiceState()).toBe('error')
      expect(unavailable).toHaveBeenCalledExactlyOnceWith('unavailable')
      expect(end).not.toHaveBeenCalled()
      utterance.onend?.(new Event('end') as SpeechSynthesisEvent)
      expect(end).not.toHaveBeenCalled()
      expect(cancel).toHaveBeenCalled()
    } finally { stopCompanionSpeech(); vi.useRealTimers(); vi.unstubAllGlobals() }
  })
  it('keeps synchronized projections alive when managed TTS falls back to the system voice', async () => {
    class FakeUtterance {
      voice: SpeechSynthesisVoice | null = null
      lang = ''
      rate = 1
      onstart: (() => void) | null = null
      onend: (() => void) | null = null
      onerror: (() => void) | null = null
      constructor(readonly text: string) {}
    }
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    const speak = vi.fn()
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(),
      speak,
      getVoices: () => [] as SpeechSynthesisVoice[],
    })
    const provider: TtsProvider = {
      id: 'unavailable-managed', isProductionVoice: true,
      async *stream() { throw new Error('TTS_STREAM_UNAVAILABLE') },
    }
    const states: string[] = []
    const unsubscribe = subscribeCompanionVoice((next) => states.push(next))
    const onPlaybackStart = vi.fn()

    await expect(speakCompanion('系统语音接管后继续板书', provider, undefined, onPlaybackStart)).resolves.toBe('system_offline')
    expect(getCompanionPlaybackLocalOnly()).toBe(true)
    expect(states).not.toContain('error')
    const utterance = speak.mock.calls[0][0] as FakeUtterance
    utterance.onstart?.()
    expect(onPlaybackStart).toHaveBeenCalledOnce()

    unsubscribe()
    stopCompanionSpeech()
    vi.unstubAllGlobals()
  })

  it('labels a known local Windows female voice separately from a generic system voice', async () => {
    class FakeUtterance {
      voice: SpeechSynthesisVoice | null = null
      lang = ''
      rate = 1
      onend: (() => void) | null = null
      onerror: (() => void) | null = null
      constructor(readonly text: string) {}
    }
    const speak = vi.fn()
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    vi.stubGlobal('speechSynthesis', {
      cancel: vi.fn(), speak,
      getVoices: () => [{ name: 'Microsoft Xiaoxiao', voiceURI: 'windows-xiaoxiao', lang: 'zh-CN', localService: true }] as SpeechSynthesisVoice[]
    })
    await expect(speakCompanion('离线测试')).resolves.toBe('system_female_offline')
    expect(speak).toHaveBeenCalledOnce()
    stopCompanionSpeech()
    vi.unstubAllGlobals()
  })
})

describe('playback privacy begins at accepted audio, not provider preparation', () => {
  afterEach(() => { stopCompanionSpeech(); vi.unstubAllGlobals() })
  it('ignores cancellation from an old speech owner but cancels the current preparing stream', async () => {
    const signals: AbortSignal[] = [], releases: (() => void)[] = []
    vi.stubGlobal('AudioContext', class { currentTime = 0; state = 'running'; destination = {}; close = vi.fn().mockResolvedValue(undefined) })
    const provider: TtsProvider = { id: 'owned-managed', isProductionVoice: true,
      async *stream(request) {
        signals.push(request.signal)
        await new Promise<void>(resolve => releases.push(resolve))
        if (request.signal.aborted) return
        throw new Error('NO_AUDIO_FOR_TEST')
      } }
    const first = speakCompanion('旧恢复声音', provider, undefined, undefined, { speechKey: 'old', managedVoiceRequired: true })
    const second = speakCompanion('新回复', provider, undefined, undefined, { speechKey: 'new', managedVoiceRequired: true })
    expect(signals[0].aborted).toBe(true)
    stopCompanionSpeech('old')
    expect(signals[1].aborted).toBe(false)
    expect(getCompanionVoiceState()).toBe('preparing')
    stopCompanionSpeech('new')
    expect(signals[1].aborted).toBe(true)
    releases.forEach(resolve => resolve())
    await expect(first).resolves.toBe('text_only')
    await expect(second).resolves.toBe('text_only')
  })
  it('keeps pending synthesis protected and publishes public ownership only at its first PCM frame', async () => {
    const sources: EventTarget[] = [], privacy: boolean[] = []
    class Context {
      currentTime = 0; state = 'running'; destination = {}
      close = vi.fn().mockResolvedValue(undefined)
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return { duration: length / sampleRate, getChannelData: () => new Float32Array(length) }
      }
      createBufferSource() {
        let renderEnd = 0
        const source = Object.assign(new EventTarget(), { buffer: null as unknown, connect: vi.fn(), stop: vi.fn(), start: vi.fn() })
        source.start.mockImplementation((when: number) => { renderEnd = when + (source.buffer as AudioBuffer).duration })
        source.addEventListener('ended', () => { this.currentTime = renderEnd }, { once: true })
        sources.push(source); return source
      }
    }
    vi.stubGlobal('AudioContext', Context)
    let ready: (() => void) | undefined
    const provider: TtsProvider = { id: 'pending-managed', isProductionVoice: true,
      async *stream() {
        await new Promise<void>(resolve => { ready = resolve })
        yield { pcm16: new Int16Array([1, 2]).buffer, sampleRate: 24_000 }
      } }
    const unsubscribe = subscribeCompanionVoice(state => { if (state === 'speaking') privacy.push(getCompanionPlaybackLocalOnly()) })
    try {
      const speaking = speakCompanion('等音频真正到达', provider)
      expect(getCompanionPlaybackLocalOnly()).toBe(true)
      expect(getCompanionVoiceState()).toBe('preparing')
      expect(privacy).toHaveLength(0)
      ready?.()
      await vi.waitFor(() => expect(sources).toHaveLength(1))
      expect(privacy.at(-1)).toBe(false)
      sources[0].dispatchEvent(new Event('ended'))
      await expect(speaking).resolves.toBe('managed')
    } finally { unsubscribe() }
  })

  it('never marks a denied managed provider offline fallback as public', async () => {
    const local = { name: 'Local', lang: 'zh-CN', localService: true } as SpeechSynthesisVoice
    vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    const speak = vi.fn()
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak, getVoices: () => [local] })
    const provider: TtsProvider = { id: 'denied', isProductionVoice: true, localFallbackOnly: true,
      async *stream() { throw new Error('POLICY_DENIED') } }
    await expect(speakCompanion('仅本机允许的回复', provider)).resolves.toBe('system_offline')
    expect(speak.mock.calls[0][0].voice).toBe(local)
    expect(getCompanionPlaybackLocalOnly()).toBe(true)
  })
})
