import { describe, expect, it, vi } from 'vitest'
import { QwenOutputRelay } from '../../main/qwen-output-relay'
import { QwenRealtimeSequencedOutput } from './qwenRealtimeSequencedOutput'
import type { QwenOutputEnvelope } from '../../shared/qwenOutputBridge'

const scope = { sessionId: 'synthetic_session', childId: 'synthetic_child', agentTurnId: 'synthetic_turn',
  callId: 'call_synthetic', responseId: 'resp_synthetic', voice: 'Tina' }
const part = (type: string, event_id: string) => ({ type, event_id, response_id: scope.responseId,
  item_id: 'item_synthetic', output_index: 0, content_index: 0 })
function source(): QwenOutputEnvelope[] {
  const events: QwenOutputEnvelope[] = []
  const relay = new QwenOutputRelay({ scope, localOnly: false, cloudTransmissionAllowed: true, isCurrent: () => true,
    emit: e => events.push(e), onFailure: () => { throw Error('Source fixture failed') } })
  for (let i = 0; i < 2; i++) relay.receive({ ...part('response.audio.delta', `event_synthetic_${i}`), delta: 'AAAAAAAAAAA=' })
  relay.receive(part('response.audio.done', 'event_synthetic_audio_done'))
  relay.receive({ type: 'response.done', event_id: 'event_synthetic_end', response: { id: scope.responseId,
    voice: 'Tina', status: 'completed', output: [{ id: 'item_synthetic', type: 'message', role: 'assistant',
      status: 'completed', content: [{ type: 'audio' }] }] } })
  return events
}
async function consume(events: unknown[]) {
  const close = vi.fn(), output = new QwenRealtimeSequencedOutput({ scope, isCurrent: () => true, close })
  const reading = (async () => {
    let samples = 0
    for await (const chunk of output.stream({ text: '合成回答。', requestId: 'synthetic_request', signal: new AbortController().signal })) samples += chunk.pcm16.byteLength / 2
    return samples
  })()
  for (const event of events) output.receive(event)
  output.transportEnded()
  try { return await reading } finally { expect(close).toHaveBeenCalledOnce() }
}
describe('independent renderer bridge integrity', () => {
  it('accepts exact scoped sequence and cumulative sample manifest through the current PCM candidate', async () => {
    expect(await consume(source())).toBe(8)
  })
  it.each(['drop_first', 'drop_middle', 'duplicate', 'reverse', 'drop_last'])('rejects %s even when supplier EOS remains available', async mode => {
    const events = source()
    if (mode === 'drop_first') events.shift()
    if (mode === 'drop_middle') events.splice(1, 1)
    if (mode === 'drop_last') events.pop()
    if (mode === 'duplicate') events.splice(1, 0, events[0])
    if (mode === 'reverse') events.reverse()
    await expect(consume(events)).rejects.toThrow('QWEN_')
  })
  it.each(['sessionId', 'childId', 'agentTurnId', 'callId', 'responseId', 'voice'])('rejects another %s route', async field => {
    const events = source(); events[1].scope = { ...events[1].scope, [field]: 'different_owner' }
    await expect(consume(events)).rejects.toThrow('QWEN_BRIDGE_SCOPE_MISMATCH')
  })
  it.each(['sequence', 'frameCount', 'pcmBytes', 'sampleCount'])('rejects tampered %s on the terminal', async field => {
    const events = source(); Object.assign(events[3], { [field]: 999 })
    await expect(consume(events)).rejects.toThrow('QWEN_BRIDGE_')
  })
  it('rejects PCM replacement with the same frame count but different sample length', async () => {
    const events = source(); events[1].event.delta = 'AAAA'
    await expect(consume(events)).rejects.toThrow('QWEN_BRIDGE_COUNTS_MISMATCH')
  })
  it('does not resurrect the retired renderer route', async () => {
    const close = vi.fn(), output = new QwenRealtimeSequencedOutput({ scope, isCurrent: () => true, close })
    output.retire()
    expect(output.receive(source()[0])).toBe('retired')
    expect(close).toHaveBeenCalledOnce()
  })
  it('retires without leaking an exceptional transport object', async () => {
    const event = Object.defineProperty({}, 'type', { get: () => { throw Error('Synthetic secret') } })
    await expect(consume([event])).rejects.toThrow('QWEN_BRIDGE_ENVELOPE_INVALID')
  })
})
