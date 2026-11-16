// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }))
const wire = vi.hoisted(() => ({ mode: 'normal', sent: [] as Record<string, unknown>[], sockets: 0, closed: 0 }))
vi.mock('ws', async () => {
  const { EventEmitter } = await import('node:events')
  class Socket extends EventEmitter {
    static OPEN = 1
    readyState = 1
    responses = 0
    constructor() { super(); wire.sockets++; queueMicrotask(() => this.emit('message', Buffer.from(JSON.stringify({
      type: 'session.created', session: { id: 'synthetic_supplier_session', model: 'qwen3.8-omni-flash-realtime' },
    })), false)) }
    terminate() { if (this.readyState === 1) { this.readyState = 3; wire.closed++ } }
    send(encoded: string) {
      const event = JSON.parse(encoded); wire.sent.push(event)
      const emit = (body: unknown) => this.emit('message', Buffer.from(JSON.stringify(body)), false)
      if (event.type === 'session.update') queueMicrotask(() => emit({ type: 'session.updated', session: {
        id: 'synthetic_supplier_session', model: 'qwen3.8-omni-flash-realtime',
        audio: { output: { voice: 'Tina', format: { type: 'pcm', sample_rate: 24000 } } },
      } }))
      if (event.type === 'response.create') queueMicrotask(() => {
        if (++this.responses === 1) {
          emit({ type: 'response.created', response: { id: 'resp_synthetic_tool' } })
          emit({ type: 'response.done', response: { id: 'resp_synthetic_tool', status: 'completed', output: [{
            type: 'function_call', name: 'receive_public_reply', status: 'completed', call_id: 'synthetic_supplier_call', arguments: '{}' }] } }); return
        }
        emit({ type: 'response.created', response: { id: 'resp_synthetic_output' } })
        if (wire.mode === 'stalled') return
        const fields = { response_id: 'resp_synthetic_output', item_id: 'item_synthetic_audio', output_index: 0, content_index: 0 }
        emit({ type: 'response.audio.delta', event_id: 'event_synthetic_pcm', ...fields, delta: Buffer.alloc(4800).toString('base64') })
        if (wire.mode === 'disconnected') { this.emit('close'); return }
        emit({ type: 'response.audio_transcript.done', ...fields, transcript: wire.mode === 'changed' ? '供应商另说了一句。' : '我们接着看下一步。' })
        emit({ type: 'response.audio.done', event_id: 'event_synthetic_audio_done', ...fields })
        emit({ type: 'response.done', event_id: 'event_synthetic_done', response: { id: fields.response_id,
          status: 'completed', output: [{ id: fields.item_id, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'audio' }] }] } })
      })
    }
  }
  return { default: Socket }
})
import { PublicReplySpeechService } from './public-reply-speech'
const source = { sessionId: 'synthetic_session', childId: 'synthetic_child', agentTurnId: 'synthetic_turn' }
const config = { enabled: true, apiKey: 'synthetic-key-never-real', endpoint: 'https://ws-synthetic.cn-beijing.maas.aliyuncs.com', voice: 'Tina', model: 'qwen3.8-omni-flash-realtime' as const }
const services: PublicReplySpeechService[] = []
function setup() {
  let reserved = false, bound = false, current = true
  const operations: string[] = [], retired: string[] = []
  const context = vi.fn(async () => ({ status: 'eligible', child_id: source.childId, storage_generation: 'synthetic_generation' }))
  const service = new PublicReplySpeechService({ context, config: async () => config, isCurrent: () => current,
    onRetired: id => { retired.push(id) }, authority: async (command: NativeRealtimeOutputCommand) => {
      operations.push(command.operation)
      if (command.operation === 'reserve') { reserved = true; return { state: 'reserved', scope: command,
        response_text: '我们接着看下一步。', local_only: false, cloud_transmission_allowed: true } }
      if (command.operation === 'bind') { bound = true; return { state: 'bound', dispatch_allowed: true } }
      return { state: bound ? 'bound' : reserved ? 'reserved' : 'revoked', dispatch_allowed: false,
        generation_completed: command.operation === 'generated', delivery_status: command.operation }
    } })
  services.push(service)
  return { service, operations, retired, context, revoke: () => { current = false } }
}
beforeEach(() => { wire.mode = 'normal'; wire.sent = []; wire.sockets = wire.closed = 0 })
afterEach(async () => { await Promise.all(services.splice(0).map(service => service.close())) })

describe('Runtime public reply outlet', () => {
  it('sends only Runtime text once and separates EOS from persisted playback', async () => {
    const { service, operations } = setup()
    const opened = await service.open(source); expect(opened.status).toBe('eligible')
    if (opened.status !== 'eligible') throw Error('missing lease')
    const result = await service.generate(opened.leaseId)
    expect(result).toMatchObject({ sampleCount: 2400, frameCount: 1, generationCompleted: true, renderedCompleted: false, voice: 'Tina' })
    expect(wire.sent.filter(e => e.type === 'response.create')).toHaveLength(2)
    expect(wire.sent.filter(e => e.type === 'conversation.item.create')).toContainEqual({ type: 'conversation.item.create',
      item: { type: 'function_call_output', call_id: 'synthetic_supplier_call', output: JSON.stringify({ response_text: '我们接着看下一步。' }) } })
    expect(operations).not.toContain('rendered')
    await service.settle(opened.leaseId, 'started'); await service.settle(opened.leaseId, 'rendered')
    expect(operations.slice(-1)).toEqual(['rendered'])
    await expect(service.generate(opened.leaseId)).rejects.toThrow('ALREADY_ATTEMPTED')
    expect(wire.sockets).toBe(wire.closed)
  })
  it.each(['changed', 'disconnected'])('withholds samples on %s and never retries', async mode => {
    wire.mode = mode
    const { service, operations } = setup(); const opened = await service.open(source)
    if (opened.status !== 'eligible') throw Error('missing lease')
    await expect(service.generate(opened.leaseId)).rejects.toThrow('UNCONFIRMED')
    expect(operations).not.toContain('generated'); expect(operations).not.toContain('rendered')
    expect(wire.sent.filter(e => e.type === 'response.create')).toHaveLength(2)
    expect(wire.sockets).toBe(wire.closed)
  })
  it('cancels an in-flight reply on revocation without promoting it', async () => {
    wire.mode = 'stalled'
    const { service, operations, revoke } = setup(); const opened = await service.open(source)
    if (opened.status !== 'eligible') throw Error('missing lease')
    const pending = service.generate(opened.leaseId)
    const rejected = expect(pending).rejects.toThrow('UNCONFIRMED')
    await vi.waitFor(() => expect(operations).toContain('bind'))
    revoke(); await rejected
    expect(operations).not.toContain('generated'); expect(wire.sockets).toBe(wire.closed)
  })
  it('never sends a renderer body or restarts an uncertain output', async () => {
    const { service, context } = setup()
    await expect(service.open({ ...source, text: '本地秘密' } as typeof source)).rejects.toThrow('SOURCE_INVALID')
    context.mockResolvedValue({ status: 'already_attempted', child_id: source.childId, storage_generation: 'synthetic_generation' })
    expect(await service.open(source)).toEqual({ status: 'unconfirmed' }); expect(wire.sockets).toBe(0)
  })
})
