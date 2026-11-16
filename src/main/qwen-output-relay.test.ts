import { describe, expect, it, vi } from 'vitest'
import { QwenOutputRelay } from './qwen-output-relay'
import type { QwenOutputEnvelope } from '../shared/qwenOutputBridge'

const scope = { sessionId: 'synthetic_session', childId: 'synthetic_child', agentTurnId: 'synthetic_turn',
  callId: 'call_synthetic', responseId: 'resp_synthetic', voice: 'Tina' }
const part = (type: string, event_id: string, extra: Record<string, unknown> = {}) => ({ type, event_id,
  response_id: scope.responseId, item_id: 'item_synthetic', output_index: 0, content_index: 0, ...extra })
const delta = (event_id = 'event_synthetic_delta') => part('response.audio.delta', event_id, { delta: 'AAAAAAAAAAA=' })
const end = () => ({ type: 'response.done', event_id: 'event_synthetic_end', response: { id: scope.responseId,
  status: 'completed', voice: 'Tina', output: [{ id: 'item_synthetic', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'audio' }] }] } })
function setup(patch: Record<string, unknown> = {}) {
  const events: QwenOutputEnvelope[] = [], failure = vi.fn(), owner = { current: true }
  const relay = new QwenOutputRelay({ scope, localOnly: false, cloudTransmissionAllowed: true,
    isCurrent: () => owner.current, emit: event => events.push(event), onFailure: failure, ...patch })
  return { relay, events, failure, owner }
}
describe('owned main output relay qualification', () => {
  it('binds the complete runtime scope and independently counts every source frame before forwarding', () => {
    const { relay, events } = setup()
    relay.receive(delta()); relay.receive(delta('event_synthetic_second'))
    relay.receive(part('response.audio.done', 'event_synthetic_audio_done')); relay.receive(end())
    expect(events.map(e => [e.sequence, e.frameCount, e.pcmBytes, e.sampleCount])).toEqual([[1, 1, 8, 4], [2, 2, 16, 8], [3, 2, 16, 8], [4, 2, 16, 8]])
    expect(events.every(e => JSON.stringify(e.scope) === JSON.stringify(scope))).toBe(true)
    expect(relay.receive(delta())).toBe('retired')
  })
  it.each([{ localOnly: true }, { cloudTransmissionAllowed: false }])('denies protected output before forwarding (%j)', patch => {
    expect(() => setup(patch)).toThrow('QWEN_OUTPUT_LOCAL_ONLY')
  })
  it.each(['foreign', 'duplicate', 'audio_after_done', 'bad_pcm', 'voice_changed', 'failed', 'tool'])('fails the owned route without leaking data (%s)', mode => {
    const { relay, events, failure } = setup()
    relay.receive(delta())
    if (mode === 'foreign') relay.receive(part('response.audio.delta', 'event_other_response', { response_id: 'resp_foreign', delta: 'AAAA' }))
    if (mode === 'duplicate') relay.receive(delta())
    if (mode === 'audio_after_done') { relay.receive(part('response.audio.done', 'event_synthetic_audio_done')); relay.receive(delta('event_synthetic_late')) }
    if (mode === 'bad_pcm') relay.receive(part('response.audio.delta', 'event_invalid_pcm', { delta: 'AA==' }))
    if (mode === 'tool') relay.receive(part('response.function_call_arguments.done', 'event_unexpected_tool'))
    if (mode === 'voice_changed' || mode === 'failed') {
      relay.receive(part('response.audio.done', 'event_synthetic_audio_done'))
      const event = end(); Object.assign(event.response, mode === 'failed' ? { status: 'failed' } : { voice: 'other' }); relay.receive(event)
    }
    const count = events.length
    expect(failure).toHaveBeenCalledOnce()
    expect(failure.mock.calls[0]).toHaveLength(1)
    expect(failure.mock.calls[0][0]).toMatch(/^QWEN_OUTPUT_[A-Z_]+$/)
    expect(relay.receive(end())).toBe('retired')
    expect(events).toHaveLength(count)
  })
  it.each(['retire', 'transportEnd', 'owner', 'consumer'])('retires incomplete sources once (%s)', mode => {
    const { relay, failure, owner } = setup(mode === 'consumer' ? { emit: () => { throw Error('Secret synthetic payload') } } : {})
    if (mode === 'retire') relay.retire()
    if (mode === 'transportEnd') relay.transportEnded()
    if (mode === 'owner') owner.current = false
    relay.receive(delta()); relay.transportEnded(); relay.retire()
    expect(failure).toHaveBeenCalledOnce()
    expect(JSON.stringify(failure.mock.calls)).not.toContain('Secret')
  })
  it('does not share mutable grant or event objects with a consumer', () => {
    const owned = { ...scope }, { relay, events } = setup({ scope: owned })
    const event = delta(); relay.receive(event)
    owned.childId = 'synthetic_other_child'; Object.assign(event, { delta: 'AA==' })
    expect(events[0].scope.childId).toBe(scope.childId)
    expect(events[0].event.delta).toBe('AAAAAAAAAAA=')
  })
  it('does not accept reentrant events while a consumer is receiving an envelope', () => {
    const failure = vi.fn(), events: QwenOutputEnvelope[] = []
    const relay = new QwenOutputRelay({ scope, localOnly: false, cloudTransmissionAllowed: true,
      isCurrent: () => true, onFailure: failure, emit: event => {
        events.push(event)
        relay.receive(delta('event_reentrant_delta'))
      } })
    relay.receive(delta())
    expect(events).toHaveLength(1)
    expect(failure).toHaveBeenCalledWith('QWEN_OUTPUT_REENTRANT')
  })
  it('closes the route when a grant is revoked before another frame', () => {
    const failure = vi.fn(), events: QwenOutputEnvelope[] = []
    const grant = { scope, localOnly: false, cloudTransmissionAllowed: true, isCurrent: () => true,
      emit: (event: QwenOutputEnvelope) => events.push(event), onFailure: failure }
    const relay = new QwenOutputRelay(grant)
    relay.receive(delta()); grant.cloudTransmissionAllowed = false
    relay.receive(delta('event_after_revocation'))
    expect(events).toHaveLength(1)
    expect(failure).toHaveBeenCalledWith('QWEN_OUTPUT_LOCAL_ONLY')
  })
})
