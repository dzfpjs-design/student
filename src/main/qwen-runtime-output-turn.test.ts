import { describe, expect, it, vi } from 'vitest'
import type { NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'
import { QwenRuntimeOutputTurn } from './qwen-runtime-output-turn'

const identity = { session_id: 'synthetic_session', child_id: 'synthetic_child', agent_turn_id: 'synthetic_turn',
  connection_id: 'synthetic_connection', call_id: 'synthetic_call', voice: 'Tina', permit_id: 'synthetic_permit',
  storage_generation: 'synthetic_generation' }
const updated = () => ({ type: 'session.updated', session: { turn_detection: null,
  audio: { output: { voice: 'Tina', format: { type: 'pcm', sample_rate: 24000 } } } } })
const toolDone = () => ({ type: 'response.done', response: { id: 'resp_synthetic_tool', status: 'completed', output: [
  { type: 'function_call', name: 'handle_child_turn', call_id: 'synthetic_call', status: 'completed' },
] } })
const created = () => ({ type: 'response.created', response: { id: 'resp_synthetic_output' } })
const delta = () => ({ type: 'response.audio.delta', event_id: 'event_synthetic_delta', response_id: 'resp_synthetic_output',
  item_id: 'item_synthetic_audio', output_index: 0, content_index: 0, delta: Buffer.alloc(4800).toString('base64') })
const audioDone = () => ({ ...delta(), type: 'response.audio.done', event_id: 'event_synthetic_audio_done', delta: undefined })
const done = () => ({ type: 'response.done', event_id: 'event_synthetic_response_done', response: {
  id: 'resp_synthetic_output', voice: 'Tina', status: 'completed', output: [{ id: 'item_synthetic_audio', type: 'message',
    role: 'assistant', status: 'completed', content: [{ type: 'audio' }] }],
} })

function caseFor() {
  let current = true
  const authority = vi.fn(async (command: NativeRealtimeOutputCommand): Promise<Record<string, unknown>> => {
    if (command.operation === 'reserve') return { state: 'reserved', response_text: '合成的原Runtime回复。', local_only: false, cloud_transmission_allowed: true }
    if (command.operation === 'retire') return { state: 'retired', dispatch_allowed: false }
    return { state: 'bound', response_id: command.response_id, dispatch_allowed: command.operation === 'bind' }
  })
  const send = vi.fn(), emit = vi.fn(), onFailure = vi.fn()
  const turn = new QwenRuntimeOutputTurn({ identity, authority, send, emit, onFailure, isCurrent: () => current })
  return { turn, authority, send, emit, onFailure, retireOwner: () => { current = false } }
}

describe('main Runtime-qualified original tool output', () => {
  it('dispatches the original Runtime text once, binds the provider response and retires without claiming rendering', async () => {
    const c = caseFor()
    expect(await c.turn.start(updated(), toolDone())).toBe(true)
    expect(await c.turn.start(updated(), toolDone())).toBe(false)
    expect(c.send.mock.calls.map(([event]) => event.type)).toEqual(['conversation.item.create', 'response.create'])
    expect(JSON.parse(c.send.mock.calls[0][0].item.output)).toEqual({ response_text: '合成的原Runtime回复。' })
    await c.turn.receive(created()); await c.turn.receive(delta()); await c.turn.receive(audioDone()); await c.turn.receive(done())
    expect(c.emit).toHaveBeenCalledTimes(3)
    expect(c.emit.mock.calls.map(([event]) => event.sequence)).toEqual([1, 2, 3])
    expect(c.authority.mock.calls.map(([command]) => command.operation)).toEqual(['reserve', 'bind', 'inspect', 'inspect', 'inspect', 'retire'])
    expect(c.onFailure).not.toHaveBeenCalled()
    expect(c.send.mock.calls.filter(([event]) => event.type === 'response.create')).toHaveLength(1)
    await c.turn.transportEnded(); await c.turn.receive(delta())
    expect(c.emit).toHaveBeenCalledTimes(3)
  })

  it.each(['automatic_response', 'voice_changed', 'call_changed', 'failed_tool'])('does not export text for %s', async mutation => {
    const c = caseFor(), session = updated(), tool = toolDone()
    if (mutation === 'automatic_response') Object.assign(session.session, { turn_detection: { type: 'semantic_vad' } })
    if (mutation === 'voice_changed') session.session.audio.output.voice = 'OtherVoice'
    if (mutation === 'call_changed') tool.response.output[0].call_id = 'different_call'
    if (mutation === 'failed_tool') tool.response.status = 'failed'
    expect(await c.turn.start(session, tool)).toBe(false)
    expect(c.send).not.toHaveBeenCalled(); expect(c.authority).not.toHaveBeenCalled()
  })

  it('does not retry after uncertain reserve or cloud send', async () => {
    for (const stage of ['reserve', 'send']) {
      const c = caseFor()
      if (stage === 'reserve') c.authority.mockRejectedValueOnce(Error('private synthetic detail'))
      else c.send.mockImplementationOnce(() => { throw Error('private synthetic detail') })
      expect(await c.turn.start(updated(), toolDone())).toBe(false)
      expect(await c.turn.start(updated(), toolDone())).toBe(false)
      expect(c.authority.mock.calls.filter(([command]) => command.operation === 'reserve')).toHaveLength(1)
      expect(c.send.mock.calls.filter(([event]) => event.type === 'response.create')).toHaveLength(0)
      expect(JSON.stringify(c.onFailure.mock.calls)).not.toContain('private synthetic detail')
    }
  })

  it.each(['reserve', 'bind', 'inspect'])('retires ownership changed while awaiting %s', async stage => {
    const c = caseFor(), original = c.authority.getMockImplementation()!
    c.authority.mockImplementation(async command => {
      const result = await original(command)
      if (command.operation === stage) c.retireOwner()
      return result
    })
    await c.turn.start(updated(), toolDone())
    if (stage !== 'reserve') await c.turn.receive(created())
    if (stage === 'inspect') await c.turn.receive(delta())
    expect(c.emit).not.toHaveBeenCalled()
    expect(c.authority.mock.calls.filter(([command]) => command.operation === 'retire')).toHaveLength(1)
  })

  it('buffers media during real bind without publishing it before the receipt', async () => {
    const c = caseFor()
    await c.turn.start(updated(), toolDone())
    let resolve!: (value: Record<string, unknown>) => void
    c.authority.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    const pending = c.turn.receive(created()), frame = c.turn.receive(delta())
    await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
    expect(c.emit).not.toHaveBeenCalled()
    resolve({ state: 'bound', response_id: 'resp_synthetic_output', dispatch_allowed: true })
    await pending; await frame
    expect(c.emit).toHaveBeenCalledTimes(1)
    await c.turn.cancel()
  })

  it('cancels immediately during bind and never publishes the delayed frame', async () => {
    const c = caseFor()
    await c.turn.start(updated(), toolDone())
    let resolve!: (value: Record<string, unknown>) => void
    c.authority.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    const pending = c.turn.receive(created()), frame = c.turn.receive(delta())
    await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
    await c.turn.cancel()
    expect(c.send.mock.calls.filter(([event]) => event.type === 'response.cancel')).toHaveLength(1)
    resolve({ state: 'bound', response_id: 'resp_synthetic_output', dispatch_allowed: true })
    await pending; await frame
    expect(c.emit).not.toHaveBeenCalled()
  })

  it('does not retire before an in-flight reservation settles, or let it dispatch after cancellation', async () => {
    const c = caseFor()
    let resolve!: (value: Record<string, unknown>) => void
    c.authority.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    const start = c.turn.start(updated(), toolDone()), cancelled = c.turn.cancel()
    expect(c.send).not.toHaveBeenCalled()
    expect(c.authority.mock.calls.map(([command]) => command.operation)).toEqual(['reserve'])
    resolve({ state: 'reserved', response_text: '合成迟到回复。', local_only: false, cloud_transmission_allowed: true })
    await cancelled
    expect(await start).toBe(false)
    expect(c.send).not.toHaveBeenCalled()
    expect(c.authority.mock.calls.map(([command]) => command.operation)).toEqual(['reserve', 'retire'])
  })

  it('ends an incomplete stream at its bounded deadline without another response request', async () => {
    vi.useFakeTimers()
    const c = caseFor()
    try {
      await c.turn.start(updated(), toolDone())
      await vi.advanceTimersByTimeAsync(90_000)
      expect(c.onFailure).toHaveBeenCalledWith('QWEN_RUNTIME_OUTPUT_TIMEOUT')
      expect(c.send.mock.calls.filter(([event]) => event.type === 'response.create')).toHaveLength(1)
      expect(c.send.mock.calls.filter(([event]) => event.type === 'response.cancel')).toHaveLength(1)
      await c.turn.receive(created()); expect(c.emit).not.toHaveBeenCalled()
    } finally { await c.turn.cancel(); vi.useRealTimers() }
  })

  it.each(['source_revoked', 'receipt_lost', 'response_repeated', 'media_before_created', 'session_changed', 'unexpected_tool'])('does not complete after %s', async failure => {
    const c = caseFor()
    await c.turn.start(updated(), toolDone())
    if (failure !== 'media_before_created') await c.turn.receive(created())
    if (failure === 'source_revoked') c.authority.mockResolvedValueOnce({ state: 'revoked', dispatch_allowed: false })
    if (failure === 'receipt_lost') c.authority.mockRejectedValueOnce(Error('synthetic receipt lost'))
    await c.turn.receive(failure === 'response_repeated' ? created() : failure === 'session_changed' ? updated()
      : failure === 'unexpected_tool' ? { type: 'response.function_call_arguments.done' } : delta())
    expect(c.emit).not.toHaveBeenCalled()
    expect(c.onFailure).toHaveBeenCalled()
    await c.turn.cancel()
    expect(c.send.mock.calls.filter(([event]) => event.type === 'response.create')).toHaveLength(1)
  })
})
