import { afterEach, describe, expect, it, vi } from 'vitest'
import { LocalAgentStreamingTtsProvider, PcmPlaybackCache, speakCompanion, stopCompanionSpeech } from './companionVoice'
import { resetRuntimeCache } from './api'

afterEach(() => { stopCompanionSpeech(); resetRuntimeCache(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

function frame(rate: number, payload: Uint8Array): Uint8Array {
  const value = new Uint8Array(8 + payload.length), view = new DataView(value.buffer)
  view.setUint32(0, rate, true); view.setUint32(4, payload.length, true); value.set(payload, 8)
  return value
}

const pcm = frame(24_000, new Uint8Array([1, 0, 255, 255]))
const terminal = (patch: Record<string, unknown> = {}): Uint8Array => frame(0, new TextEncoder().encode(JSON.stringify({
  type: 'generation_completed', request_id: 'synthetic-owned-request', frame_count: 1, pcm_bytes: 4, sample_count: 2, ...patch,
})))

async function read(parts: Uint8Array[], type = 'application/x-hsc-pcm16-frame-v2'): Promise<number> {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
    start(controller) { for (const part of parts) controller.enqueue(part); controller.close() },
  }), { headers: { 'Content-Type': type } })))
  let frames = 0
  const provider = new LocalAgentStreamingTtsProvider('synthetic-session')
  for await (const _ of provider.stream({ text: '一般指导', requestId: 'synthetic-owned-request', signal: new AbortController().signal })) frames++
  return frames
}

describe('managed PCM generation boundary', () => {
  it('does not turn an ordinary HTTP EOF after a valid frame into a full reply', async () => {
    await expect(read([pcm])).rejects.toThrow('TTS_STREAM_COMPLETION_MISSING')
  })

  it('accepts only matched terminal counts even with split headers and terminal body', async () => {
    const end = terminal()
    await expect(read([pcm.slice(0, 3), pcm.slice(3), end.slice(0, 9), end.slice(9)])).resolves.toBe(1)
    const [, request] = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/voice/synthesize/stream'))!
    expect(JSON.parse(request!.body as string).protocol).toBe('pcm16_frame_v2')
  })

  it.each([
    { request_id: 'retired-request' }, { frame_count: 2 }, { pcm_bytes: 6 }, { sample_count: 3 },
    { frame_count: 1.5 }, { type: 'delivered' }, { extra: true },
  ])('rejects invalid terminal identity and counts: %j', async patch => {
    await expect(read([pcm, terminal(patch)])).rejects.toThrow('TTS_STREAM_COMPLETION_INVALID')
  })

  it.each(['duplicate', 'audio_after'])('rejects %s terminal data', async mode => {
    await expect(read([pcm, terminal(), mode === 'duplicate' ? terminal() : pcm])).rejects.toThrow('TTS_STREAM_AFTER_COMPLETION')
  })

  it('does not silently downgrade to legacy EOF completion', async () => {
    await expect(read([pcm], 'application/octet-stream')).rejects.toThrow('TTS_STREAM_PROTOCOL_UNSUPPORTED')
  })

  it('yields the first samples before generation finishes, then cancels an abandoned reader', async () => {
    const cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) { controller.enqueue(pcm) }, cancel,
    }), { headers: { 'Content-Type': 'application/x-hsc-pcm16-frame-v2' } })))
    const stream = new LocalAgentStreamingTtsProvider('synthetic-session').stream({ text: '一般指导', requestId: 'synthetic-owned-request', signal: new AbortController().signal })[Symbol.asyncIterator]()
    expect((await stream.next()).value?.pcm16.byteLength).toBe(4)
    await stream.return?.()
    expect(cancel).toHaveBeenCalledOnce()
    expect(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.signal?.aborted).toBe(true)
  })

  it('bounds a stalled stream and aborts its transport without a second supplier call', async () => {
    vi.useFakeTimers()
    const cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel }), {
      headers: { 'Content-Type': 'application/x-hsc-pcm16-frame-v2' },
    })))
    const reading = readStalled()
    const result = expect(reading).rejects.toThrow('TTS_STREAM_TIMEOUT')
    await vi.advanceTimersByTimeAsync(30_001)
    await result
    expect(cancel).toHaveBeenCalledOnce()
    expect(fetch).toHaveBeenCalledOnce()
    expect(vi.mocked(fetch).mock.calls[0][1]?.signal?.aborted).toBe(true)
  })

  it('cancels a pending read immediately without waiting for its idle deadline', async () => {
    const cancel = vi.fn(), signal = new AbortController()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) { controller.enqueue(pcm) }, cancel,
    }), { headers: { 'Content-Type': 'application/x-hsc-pcm16-frame-v2' } })))
    const stream = new LocalAgentStreamingTtsProvider('synthetic-session').stream({ text: '一般指导', requestId: 'synthetic-owned-request', signal: signal.signal })[Symbol.asyncIterator]()
    await stream.next()
    const waiting = expect(stream.next()).rejects.toMatchObject({ name: 'AbortError' })
    signal.abort()
    await waiting
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([
    [Array.from({ length: 601 }, () => pcm), 'TTS_STREAM_LIMIT_EXCEEDED'],
    [[frame(0, new Uint8Array(2049))], 'TTS_STREAM_PROTOCOL_INVALID'],
    [[terminal({ frame_count: 0, pcm_bytes: 0, sample_count: 0 })], 'TTS_STREAM_COMPLETION_INVALID'],
    [[pcm, terminal(), new Uint8Array([1])], 'TTS_STREAM_AFTER_COMPLETION'],
  ])('rejects aggregate limits and partial trailing data', async (parts, error) => {
    await expect(read(parts as Uint8Array[])).rejects.toThrow(error as string)
  })

  it.each(['qualified', 'missing', 'renderer_stalled'])('keeps teaching completion and cache behind both contracts (%s)', async mode => {
    const qualified = mode !== 'missing'
    const sources: EventTarget[] = [], closed = vi.fn().mockResolvedValue(undefined), fallback = vi.fn()
    vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), speak: fallback, getVoices: () => [] })
    vi.stubGlobal('AudioContext', class {
      currentTime = 0; state = 'running'; destination = {}; close = closed
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return { duration: length / sampleRate, getChannelData: () => new Float32Array(length) }
      }
      createBufferSource() {
        let renderEnd = 0
        const source = Object.assign(new EventTarget(), { buffer: null, connect: vi.fn(), stop: vi.fn(), start: vi.fn() })
        source.start.mockImplementation((when: number) => { renderEnd = when + (source.buffer as unknown as AudioBuffer).duration })
        source.addEventListener('ended', () => { this.currentTime = renderEnd }, { once: true })
        sources.push(source); return source
      }
    })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (_url, request) => {
      const requestId = JSON.parse(request.body).client_action_id
      const end = terminal({ request_id: requestId })
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(pcm); if (qualified) controller.enqueue(end); controller.close()
      } }), { headers: { 'Content-Type': 'application/x-hsc-pcm16-frame-v2' } })
    }))
    const end = vi.fn(), failed = vi.fn(), cache = new PcmPlaybackCache()
    const speaking = speakCompanion('我们继续下一步。', new LocalAgentStreamingTtsProvider('synthetic-session'), cache, undefined,
      { managedVoiceRequired: true, onPlaybackEnd: end, onNotDelivered: failed })
    await vi.waitFor(() => expect(sources).toHaveLength(1))
    expect(end).not.toHaveBeenCalled()
    if (mode === 'qualified') sources[0].dispatchEvent(new Event('ended'))
    const rendered = mode === 'qualified'
    expect(await speaking).toBe(rendered ? 'managed' : 'text_only')
    expect(end).toHaveBeenCalledTimes(rendered ? 1 : 0)
    expect(failed).toHaveBeenCalledTimes(rendered ? 0 : 1)
    expect(cache.get('local-agent-streaming-tts:我们继续下一步。') !== null).toBe(rendered)
    expect(fallback).not.toHaveBeenCalled()
    expect(closed).toHaveBeenCalledOnce()
  })
})

async function readStalled(): Promise<void> {
  for await (const _ of new LocalAgentStreamingTtsProvider('synthetic-session').stream({ text: '一般指导', requestId: 'synthetic-owned-request', signal: new AbortController().signal })) { /* Consume under a bounded deadline. */ }
}
