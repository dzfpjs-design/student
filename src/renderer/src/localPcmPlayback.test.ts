import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getCompanionVoiceState, playLocalPcm, speakCompanion, stopCompanionSpeech, subscribeCompanionVoice } from './companionVoice'

type TestSource = EventTarget & { stop: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn>; renderEnd: number; context: { currentTime: number } }
let sources: TestSource[]
let contexts: { state: string; currentTime: number; close: ReturnType<typeof vi.fn> }[]
let synthesis: ReturnType<typeof vi.fn>
beforeEach(() => {
  sources = []; contexts = []; synthesis = vi.fn()
  vi.stubGlobal('speechSynthesis', { speak: synthesis, cancel: vi.fn() })
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('no network')))
  vi.stubGlobal('AudioContext', class {
    currentTime = 0; state = 'running'; destination = {}
    close = vi.fn().mockResolvedValue(undefined)
    constructor() { contexts.push(this) }
    createBuffer(_channels: number, count: number, rate: number) {
      return { duration: count / rate, getChannelData: () => new Float32Array(count) }
    }
    createBufferSource() {
      const source = Object.assign(new EventTarget(), { buffer: null as null | { duration: number }, renderEnd: 0,
        context: this, connect: vi.fn(), start: vi.fn((when: number) => { source.renderEnd = when + source.buffer!.duration }),
        stop: vi.fn(), disconnect: vi.fn() })
      sources.push(source); return source
    }
  })
})
afterEach(() => { stopCompanionSpeech(); vi.unstubAllGlobals() })

function finish(source: TestSource): void {
  source.context.currentTime = Math.max(source.context.currentTime, source.renderEnd)
  source.dispatchEvent(new Event('ended'))
}

it.each(['cooperative', 'late_frame'])('an early rendered block aborts a waiting generation and cannot affect the next owner (%s)', async mode => {
  const done = vi.fn(), failed = vi.fn()
  let release!: () => void, supplierSignal: AbortSignal | undefined, closed = 0, settled = false
  const waiting = new Promise<void>(resolve => { release = resolve })
  const playing = speakCompanion('合成协议回复', { id: 'synthetic-stalled', isProductionVoice: true,
    async *stream(request) {
      supplierSignal = request.signal
      try {
        yield { pcm16: new Int16Array(16000).buffer, sampleRate: 16000 }
        if (mode === 'cooperative') await new Promise<void>(resolve => request.signal.addEventListener('abort', () => resolve(), { once: true }))
        else await waiting
        yield { pcm16: new Int16Array([1, 2]).buffer, sampleRate: 16000 }
      } finally { closed++ }
    },
  }, undefined, undefined, { managedVoiceRequired: true, onPlaybackEnd: done, onNotDelivered: failed }).then(result => { settled = true; return result })
  try {
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    sources[0].dispatchEvent(new Event('ended'))
    await vi.waitFor(() => expect(supplierSignal?.aborted).toBe(true), { timeout: 250 })
    // A non-cooperative producer may need cleanup time, but never another frame.
    release()
    await expect(playing).resolves.toBe('text_only')
    expect(settled).toBe(true); expect(failed).toHaveBeenCalledWith('unavailable'); expect(done).not.toHaveBeenCalled()
    expect(closed).toBe(1); expect(sources).toHaveLength(1)
    const nextDone = vi.fn()
    const next = playLocalPcm(new Int16Array([1, 2]).buffer, 16000, undefined, { onPlaybackEnd: nextDone })
    await vi.waitFor(() => expect(sources).toHaveLength(2))
    finish(sources[1]); await expect(next).resolves.toBe('finished')
    expect(nextDone).toHaveBeenCalledOnce(); expect(contexts.every(context => context.close.mock.calls.length === 1)).toBe(true)
  } finally { stopCompanionSpeech(); release(); await playing }
})

// Front51 observed these finite deficits at the original tail comparison. These
// synthetic events exercise that callback; they do not certify native delivery.
it.each([2.6645352591003757e-15, 1.7763568394002505e-15])('accepts natural ended at an accumulated PCM tail with only a %s second ULP deficit', async deficit => {
  const done = vi.fn(), failed = vi.fn()
  const playing = speakCompanion('合成协议回复', { id: 'synthetic-ulp-tail', isProductionVoice: true,
    async *stream() {
      for (let index = 0; index < 6; index++) yield { pcm16: new Int16Array(8000).buffer, sampleRate: 24000 }
    },
  }, undefined, undefined, { managedVoiceRequired: true, onPlaybackEnd: done, onNotDelivered: failed })
  await vi.waitFor(() => expect(sources).toHaveLength(6))
  Object.assign(contexts[0], { sampleRate: 48000 })
  for (const source of sources.slice(0, -1)) finish(source)
  const last = sources[5]
  contexts[0].currentTime = last.renderEnd - deficit
  expect(last.renderEnd - contexts[0].currentTime).toBe(deficit)
  expect(deficit).toBeLessThan(1 / 48000)
  last.dispatchEvent(new Event('ended'))
  await expect(playing).resolves.toBe('managed')
  expect(done).toHaveBeenCalledOnce(); expect(failed).not.toHaveBeenCalled()
  expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(synthesis).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled()
})

it.each([24000, 48000])('rejects ended one full %s Hz PCM/output sample before the accumulated tail', async rate => {
  const done = vi.fn(), failed = vi.fn()
  const playing = speakCompanion('合成协议回复', { id: 'synthetic-sample-early', isProductionVoice: true,
    async *stream() {
      for (let index = 0; index < 6; index++) yield { pcm16: new Int16Array(8000).buffer, sampleRate: 24000 }
    },
  }, undefined, undefined, { managedVoiceRequired: true, onPlaybackEnd: done, onNotDelivered: failed })
  await vi.waitFor(() => expect(sources).toHaveLength(6))
  Object.assign(contexts[0], { sampleRate: 48000 })
  for (const source of sources.slice(0, -1)) finish(source)
  contexts[0].currentTime = sources[5].renderEnd - 1 / rate
  sources[5].dispatchEvent(new Event('ended'))
  await expect(playing).resolves.toBe('text_only')
  expect(done).not.toHaveBeenCalled(); expect(failed).toHaveBeenCalledWith('unavailable')
  expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(synthesis).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled()
})

it.each(['all', 'first'])('early native ended in a running context cannot acknowledge original audio (%s)', async mode => {
  const done = vi.fn()
  const playing = playLocalPcm(new Int16Array(32000).buffer, 16000, undefined, { onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(2))
  sources[0].dispatchEvent(new Event('ended'))
  if (mode === 'all') sources[1].dispatchEvent(new Event('ended'))
  else finish(sources[1])
  await expect(playing).resolves.toBe('unavailable')
  expect(done).not.toHaveBeenCalled(); expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(synthesis).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled()
})

it.each(['all', 'first'])('complete generation does not repair an early rendered block (%s)', async mode => {
  const done = vi.fn(), failed = vi.fn()
  const playing = speakCompanion('合成协议回复', { id: 'synthetic', isProductionVoice: true,
    async *stream() {
      yield { pcm16: new Int16Array(16000).buffer, sampleRate: 16000 }
      yield { pcm16: new Int16Array(16000).buffer, sampleRate: 16000 }
    },
  }, undefined, undefined, { managedVoiceRequired: true, onPlaybackEnd: done, onNotDelivered: failed })
  await vi.waitFor(() => expect(sources).toHaveLength(2))
  sources[0].dispatchEvent(new Event('ended'))
  if (mode === 'all') sources[1].dispatchEvent(new Event('ended'))
  else finish(sources[1])
  await expect(playing).resolves.toBe('text_only')
  expect(done).not.toHaveBeenCalled(); expect(failed).toHaveBeenCalledWith('unavailable')
  expect(synthesis).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled()
})

it('does not report completion from wall-clock passage without audio ended', async () => {
  const done = vi.fn(), started = vi.fn()
  const playing = playLocalPcm(new Int16Array([10, 20]).buffer, 16000, started, { onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  await new Promise(resolve => setTimeout(resolve, 30))
  expect(started).toHaveBeenCalledOnce(); expect(done).not.toHaveBeenCalled()
  finish(sources[0])
  await expect(playing).resolves.toBe('finished')
  expect(done).toHaveBeenCalledOnce(); expect(fetch).not.toHaveBeenCalled(); expect(synthesis).not.toHaveBeenCalled()
})

it.each(['suspended', 'closed'])('a late ended in a %s context cannot acknowledge local original audio', async state => {
  const done = vi.fn()
  const playing = playLocalPcm(new Int16Array(16000).buffer, 16000, undefined, { onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  contexts[0].state = state; sources[0].dispatchEvent(new Event('ended'))
  await expect(playing).resolves.toBe('unavailable')
  expect(done).not.toHaveBeenCalled(); expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(fetch).not.toHaveBeenCalled(); expect(synthesis).not.toHaveBeenCalled()
})

it.each(['suspended', 'closed'])('completed generation and late ended in a %s context cannot acknowledge a managed reply', async state => {
  const done = vi.fn(), unavailable = vi.fn()
  const playing = speakCompanion('合成协议回复', { id: 'synthetic', isProductionVoice: true,
    async *stream() { yield { pcm16: new Int16Array(24000).buffer, sampleRate: 24000 } },
  }, undefined, undefined, { managedVoiceRequired: true, onPlaybackEnd: done, onNotDelivered: unavailable })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  contexts[0].state = state; sources[0].dispatchEvent(new Event('ended'))
  await expect(playing).resolves.toBe('text_only')
  expect(done).not.toHaveBeenCalled(); expect(unavailable).toHaveBeenCalledWith('unavailable')
  expect(contexts[0].close).toHaveBeenCalledOnce(); expect(synthesis).not.toHaveBeenCalled()
})

it('cancelled audio cannot deliver a later completion or overwrite a newer state', async () => {
  const done = vi.fn()
  const playing = playLocalPcm(new Int16Array([1, 2]).buffer, 16000, undefined, { onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  stopCompanionSpeech()
  sources[0].dispatchEvent(new Event('ended'))
  await expect(playing).resolves.toBe('cancelled')
  expect(done).not.toHaveBeenCalled(); expect(getCompanionVoiceState()).not.toBe('speaking')
})

it('unavailable output does not synthesize replacement words', async () => {
  vi.stubGlobal('AudioContext', class { constructor() { throw new Error('device unavailable') } })
  await expect(playLocalPcm(new Int16Array([1]).buffer, 16000)).resolves.toBe('unavailable')
  expect(synthesis).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled()
})

it.each([new ArrayBuffer(0), new ArrayBuffer(3)])('rejects invalid PCM before opening output', async pcm => {
  await expect(playLocalPcm(pcm, 16000)).resolves.toBe('unavailable')
  expect(sources).toHaveLength(0)
})

it('releases every owned source and the context when one source cannot stop', async () => {
  const done = vi.fn()
  const playing = playLocalPcm(new Int16Array(32000).buffer, 16000, undefined, { speechKey: 'old', onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(2))
  sources[0].stop.mockImplementation(() => { throw new Error('private device detail') })
  expect(() => stopCompanionSpeech('old')).not.toThrow()
  await expect(playing).resolves.toBe('cancelled')
  expect(sources[1].stop).toHaveBeenCalledOnce()
  expect(sources.every(source => source.disconnect.mock.calls.length === 1)).toBe(true)
  expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(done).not.toHaveBeenCalled()
  const nextDone = vi.fn()
  const next = playLocalPcm(new Int16Array([1]).buffer, 16000, undefined, { speechKey: 'next', onPlaybackEnd: nextDone })
  await vi.waitFor(() => expect(sources).toHaveLength(3))
  stopCompanionSpeech('old')
  expect(getCompanionVoiceState()).toBe('speaking')
  finish(sources[2])
  await expect(next).resolves.toBe('finished')
  expect(nextDone).toHaveBeenCalledOnce()
})

it('a broken UI subscriber cannot suppress independent microphone state notifications', async () => {
  const gate = vi.fn(), done = vi.fn()
  const bad = subscribeCompanionVoice(() => { throw new Error('private subscriber detail') })
  const good = subscribeCompanionVoice(gate)
  try {
    const playing = playLocalPcm(new Int16Array([1]).buffer, 16000, undefined, { onPlaybackEnd: done })
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    expect(gate).toHaveBeenCalledWith('preparing')
    expect(gate).toHaveBeenCalledWith('speaking')
    stopCompanionSpeech()
    await expect(playing).resolves.toBe('cancelled')
    expect(gate).toHaveBeenCalledWith('interrupted')
    expect(done).not.toHaveBeenCalled()
  } finally { bad(); good() }
})

it('a generation failure releases the context even when stopping its scheduled audio throws', async () => {
  const done = vi.fn()
  const result = speakCompanion('synthetic', {
    id: 'synthetic', isProductionVoice: true,
    async *stream() {
      yield { pcm16: new Int16Array([1]).buffer, sampleRate: 16000 }
      sources[0].stop.mockImplementation(() => { throw new Error('private audio detail') })
      throw new Error('generation failed')
    },
  }, undefined, undefined, { managedVoiceRequired: true, onPlaybackEnd: done })
  await expect(result).resolves.toBe('text_only')
  expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(sources[0].disconnect).toHaveBeenCalledOnce()
  expect(done).not.toHaveBeenCalled()
  expect(synthesis).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled()
})

it('a synchronous context release and system cancellation failure cannot leave the UI speaking', async () => {
  const warnings = vi.spyOn(console, 'warn').mockImplementation(() => {})
  try {
    const playing = playLocalPcm(new Int16Array([1]).buffer, 16000)
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    contexts[0].close.mockImplementation(() => { throw new Error('private context detail') })
    vi.mocked(speechSynthesis.cancel).mockImplementation(() => { throw new Error('private system detail') })
    expect(() => stopCompanionSpeech()).not.toThrow()
    await expect(playing).resolves.toBe('cancelled')
    expect(getCompanionVoiceState()).not.toBe('speaking')
    expect(warnings.mock.calls.flat()).toEqual(expect.arrayContaining([
      'COMPANION_AUDIO_CONTEXT_RELEASE_FAILED', 'COMPANION_SYSTEM_SPEECH_CANCEL_FAILED',
    ]))
    expect(JSON.stringify(warnings.mock.calls)).not.toContain('private')
  } finally { warnings.mockRestore() }
})

it('a stalled old context release is bounded and cannot cancel the next owned playback', async () => {
  const warnings = vi.spyOn(console, 'warn').mockImplementation(() => {})
  try {
    const old = playLocalPcm(new Int16Array([1]).buffer, 16000, undefined, { speechKey: 'old' })
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    contexts[0].close.mockReturnValue(new Promise(() => {}))
    stopCompanionSpeech('old')
    const nextDone = vi.fn()
    const next = playLocalPcm(new Int16Array([2]).buffer, 16000, undefined, { speechKey: 'next', onPlaybackEnd: nextDone })
    await vi.waitFor(() => expect(sources).toHaveLength(2))
    finish(sources[1])
    await expect(next).resolves.toBe('finished')
    await expect(old).resolves.toBe('cancelled')
    expect(nextDone).toHaveBeenCalledOnce()
    expect(contexts[0].close).toHaveBeenCalledOnce()
    expect(getCompanionVoiceState()).toBe('idle')
    expect(warnings).toHaveBeenCalledWith('COMPANION_AUDIO_CONTEXT_RELEASE_FAILED')
  } finally { warnings.mockRestore() }
})

it('stops owned PCM when its original question or child lease is withdrawn during playback', async () => {
  let current = true
  const done = vi.fn()
  const playing = playLocalPcm(new Int16Array(32000).buffer, 16000, undefined,
    { isCurrent: () => current, onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(2))
  current = false
  await vi.waitFor(() => expect(contexts[0].close).toHaveBeenCalledOnce())
  await expect(playing).resolves.toBe('cancelled')
  expect(done).not.toHaveBeenCalled()
})

it('an ended event after owner withdrawal is not a current playback receipt', async () => {
  let current = true
  const done = vi.fn()
  const playing = playLocalPcm(new Int16Array([1]).buffer, 16000, undefined,
    { isCurrent: () => current, onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  current = false
  finish(sources[0])
  await expect(playing).resolves.toBe('cancelled')
  expect(done).not.toHaveBeenCalled()
})

it('stale or failed lease checks do not stop a newer owner or open another context', async () => {
  const active = playLocalPcm(new Int16Array([1]).buffer, 16000, undefined, { speechKey: 'current' })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  for (const isCurrent of [() => false, () => { throw Error('private owner error') }]) {
    await expect(playLocalPcm(new Int16Array([2]).buffer, 16000, undefined, { isCurrent })).resolves.toBe('cancelled')
  }
  expect(contexts).toHaveLength(1)
  expect(contexts[0].close).not.toHaveBeenCalled()
  expect(getCompanionVoiceState()).toBe('speaking')
  finish(sources[0])
  await expect(active).resolves.toBe('finished')
})

it('managed PCM owner withdrawal cannot deliver or silently switch to system speech', async () => {
  let current = true
  const done = vi.fn()
  const playing = speakCompanion('synthetic', { id: 'synthetic', isProductionVoice: true,
    async *stream() { yield { pcm16: new Int16Array(32000).buffer, sampleRate: 16000 } },
  }, undefined, undefined, { isCurrent: () => current, onPlaybackEnd: done })
  await vi.waitFor(() => expect(sources).toHaveLength(1))
  current = false
  await expect(playing).resolves.toBe('text_only')
  expect(contexts[0].close).toHaveBeenCalledOnce()
  expect(done).not.toHaveBeenCalled(); expect(synthesis).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})
