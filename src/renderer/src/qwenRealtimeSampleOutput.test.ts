import { afterEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeSampleOutput } from './qwenRealtimeSampleOutput'

const id = 'resp_synthetic_current', item = 'item_synthetic_audio'
let eventIndex = 0
const part = (type: string, extra: Record<string, unknown> = {}) => ({ type, event_id: `event_synthetic_${++eventIndex}`, response_id: id,
  item_id: item, output_index: 0, content_index: 0, ...extra })
const delta = (extra: Record<string, unknown> = {}) => part('response.audio.delta', { delta: btoa('\0\0\0\0'), ...extra })
const done = (extra: Record<string, unknown> = {}) => ({ type: 'response.done', event_id: `event_synthetic_${++eventIndex}`, response: { id,
  status: 'completed', voice: 'Tina', output: [{ id: item, type: 'message', status: 'completed', role: 'assistant',
    content: [{ type: 'audio', transcript: '合成回答。' }] }], ...extra } })
function setup() {
  const close = vi.fn(), owner = { current: true }
  const output = new QwenRealtimeSampleOutput({ responseId: id, voice: 'Tina', isCurrent: () => owner.current, close })
  const controller = new AbortController()
  const stream = output.stream({ text: '合成回答。', requestId: 'synthetic-request', signal: controller.signal })[Symbol.asyncIterator]()
  return { output, stream, controller, close, owner }
}
async function consume(events: unknown[]) {
  const state = setup()
  for (const event of events) state.output.receive(event)
  state.output.transportEnded()
  let samples = 0
  for (;;) { const step = await state.stream.next(); if (step.done) return samples; samples += step.value.pcm16.byteLength / 2 }
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

describe('Qwen current-response known sample qualification', () => {
  it('yields samples before generation ends, requiring both matching audio and response EOS', async () => {
    const { output, stream, close } = setup()
    output.receive(delta())
    expect((await stream.next()).value).toMatchObject({ sampleRate: 24000 })
    let ended = false
    const pending = stream.next().then(value => { ended = true; return value })
    output.receive(part('response.audio.done'))
    await Promise.resolve()
    expect(ended).toBe(false)
    output.receive(done())
    expect(await pending).toMatchObject({ done: true })
    expect(output.generationEvidence).toEqual({ responseId: id, itemId: item, sampleCount: 2,
      pcmBytes: 4, frameCount: 1, generationCompleted: true, renderedCompleted: false })
    expect(close).toHaveBeenCalledOnce()
  })
  it.each(['EOF', 'audio_done_only', 'response_done_only', 'empty'])('rejects incomplete %s', async mode => {
    const events = mode === 'empty' ? [part('response.audio.done'), done()] : [delta(),
      ...(mode === 'audio_done_only' ? [part('response.audio.done')] : []), ...(mode === 'response_done_only' ? [done()] : [])]
    await expect(consume(events)).rejects.toThrow('QWEN_SAMPLE_')
  })
  it.each([
    { response_id: 'resp_retired' }, { item_id: 'item_other' }, { output_index: 1 }, { content_index: 1 },
    { delta: 'not base64!' }, { delta: 'AA==' }, { delta: '' },
  ])('rejects identity, format or sample conflicts %j', async patch => {
    await expect(consume([delta(), delta(patch), part('response.audio.done'), done()])).rejects.toThrow('QWEN_SAMPLE_')
  })
  it.each([{ status: 'failed' }, { status: 'cancelled' }, { status: 'incomplete' }, { voice: 'other' },
    { id: 'resp_foreign' }, { output: [] }, { output: [{ id: 'other', type: 'message' }] }])('rejects unsuccessful/foreign response EOS %j', async patch => {
    await expect(consume([delta(), part('response.audio.done'), done(patch)])).rejects.toThrow('QWEN_SAMPLE_')
  })
  it.each(['duplicate_audio_done', 'audio_after_done', 'tool_before_audio'])('fails closed on %s', async mode => {
    await expect(consume([delta(), ...(mode === 'tool_before_audio' ? [part('response.function_call_arguments.done')]
      : [part('response.audio.done'), mode === 'audio_after_done' ? delta() : part('response.audio.done')]), done()])).rejects.toThrow('QWEN_SAMPLE_')
  })
  it('retired outputs cannot revive audio or change the completion snapshot', async () => {
    const { output, stream, close } = setup()
    output.receive(delta()); output.receive(part('response.audio.done')); output.receive(done())
    expect((await stream.next()).done).toBe(false)
    expect((await stream.next()).done).toBe(true)
    expect(output.receive(delta())).toBe('retired')
    expect(output.receive(done({ status: 'failed' }))).toBe('retired')
    const copy = output.generationEvidence!; copy.sampleCount = 99
    expect(output.generationEvidence?.sampleCount).toBe(2)
    expect(close).toHaveBeenCalledOnce()
  })
  it.each(['abort', 'retire', 'source_end', 'timeout'])('interrupts a pending read and closes the owned source once (%s)', async mode => {
    vi.useFakeTimers()
    const { output, stream, close, controller } = setup()
    const waiting = expect(stream.next()).rejects.toThrow()
    if (mode === 'abort') controller.abort()
    if (mode === 'retire') output.retire()
    if (mode === 'source_end') output.transportEnded()
    if (mode === 'timeout') await vi.advanceTimersByTimeAsync(30001)
    await waiting
    expect(close).toHaveBeenCalledOnce()
    expect(output.generationEvidence).toBeNull()
    expect(output.receive(delta())).toBe('retired')
  })
  it('rechecks current ownership before yielding queued PCM and never allows a second consumer', async () => {
    const { output, stream, owner, close } = setup()
    output.receive(delta()); owner.current = false
    await expect(stream.next()).rejects.toThrow('QWEN_SAMPLE_RETIRED')
    const second = output.stream({ text: '合成回答。', requestId: 'other-request', signal: new AbortController().signal })[Symbol.asyncIterator]()
    await expect(second.next()).rejects.toThrow('QWEN_SAMPLE_CONSUMER_REUSED')
    expect(close).toHaveBeenCalledOnce()
  })
  it('bounds queued samples and does not keep draining a failed source', async () => {
    const { output, stream, close } = setup()
    for (let i = 0; i < 513; i++) output.receive(delta())
    await expect(stream.next()).rejects.toThrow('QWEN_SAMPLE_QUEUE_LIMIT')
    expect(close).toHaveBeenCalledOnce()
  })
  it('consumer close after first samples retires the response and closes its owned source', async () => {
    const { output, stream, close } = setup()
    output.receive(delta()); await stream.next(); await stream.return?.()
    expect(output.receive(part('response.audio.done'))).toBe('retired')
    expect(output.generationEvidence).toBeNull()
    expect(close).toHaveBeenCalledOnce()
  })
  it('rejects replayed PCM event IDs instead of duplicating speech', async () => {
    const event = delta()
    await expect(consume([event, event, part('response.audio.done'), done()])).rejects.toThrow('QWEN_SAMPLE_EVENT_REPLAYED')
  })
  it('does not declare success when response-lane retirement fails', async () => {
    const output = new QwenRealtimeSampleOutput({ responseId: id, voice: 'Tina', isCurrent: () => true,
      close: () => { throw new Error('Synthetic route failure') } })
    output.receive(delta()); output.receive(part('response.audio.done')); output.receive(done())
    const stream = output.stream({ text: '合成回答。', requestId: 'synthetic-request', signal: new AbortController().signal })[Symbol.asyncIterator]()
    await stream.next()
    await expect(stream.next()).rejects.toThrow('QWEN_SAMPLE_SOURCE_CLOSE_FAILED')
  })
  it.each([undefined, '', 'bad id', 2])('rejects missing or invalid event identity (%j)', async event_id => {
    await expect(consume([delta({ event_id }), part('response.audio.done'), done()])).rejects.toThrow('QWEN_SAMPLE_EVENT_INVALID')
  })
})
