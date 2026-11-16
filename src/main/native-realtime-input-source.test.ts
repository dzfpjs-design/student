// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHmac } from 'node:crypto'
import { nativeRealtimeInputSourceFields, type NativeRealtimeInputSourceCommand } from '../shared/nativeRealtimeInputSource'
import { readNativeRealtimeInputSource, signNativeRealtimeInputSource } from './native-realtime-input-source'

const command: NativeRealtimeInputSourceCommand = { session_id: 'synthetic_session', child_id: 'synthetic_child',
  client_action_id: 'synthetic_action', voice_input_id: 'synthetic_voice_input', connection_id: 'synthetic_connection',
  storage_generation: 'synthetic_generation', issued_at: '2026-10-05T00:00:00.000Z' }
const { issued_at: _, ...scope } = command
const source = () => ({ scope: { ...scope }, transcript: '第一句，还有第二句。', source_hash: 'a'.repeat(64), status: 'completed',
  source_kind: 'realtime_transcript', onset_provenance: 'client_reported', local_only: true,
  arrival_status: 'not_dispatched',
  cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })
const runtime = { status: 'ready', localApiUrl: 'http://127.0.0.1:12345', sessionToken: 'synthetic-session-token' }
const sign = (body: NativeRealtimeInputSourceCommand) => signNativeRealtimeInputSource('ab'.repeat(32), body)

describe('private local original-input source read', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('uses its own purpose and canonical cross-language order', () => {
    const expected = createHmac('sha256', Buffer.from('ab'.repeat(32), 'hex'))
      .update(JSON.stringify(['native_realtime_input_source_v1', ...nativeRealtimeInputSourceFields.map(f => command[f])])).digest('hex')
    expect(sign(command)).toBe(expected)
    for (const field of nativeRealtimeInputSourceFields) expect(sign({ ...command, [field]: 'changed' })).not.toBe(expected)
  })
  it('returns the complete original locally, never a cloud or replay permit', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(source())))
    vi.stubGlobal('fetch', fetcher)
    expect(await readNativeRealtimeInputSource(command, runtime, sign)).toEqual(source())
    expect(fetcher).toHaveBeenCalledOnce()
    const options = fetcher.mock.calls[0][1]
    expect(options.redirect).toBe('error')
    const sent = JSON.parse(options.body)
    expect('transcript' in sent || 'cloud_transmission_allowed' in sent).toBe(false)
  })
  it.each(['child', 'generation', 'text_missing', 'source_hash', 'cloud_allowed', 'replay_allowed', 'extra_context', 'oversize', 'provenance'])('rejects unqualified local receipt %s', async kind => {
    const receipt = source()
    if (kind === 'child') receipt.scope.child_id = 'another_child'
    if (kind === 'generation') receipt.scope.storage_generation = 'another_generation'
    if (kind === 'text_missing') receipt.transcript = '   '
    if (kind === 'source_hash') receipt.source_hash = 'invalid'
    if (kind === 'cloud_allowed') receipt.cloud_transmission_allowed = true
    if (kind === 'replay_allowed') receipt.replay_allowed = true
    if (kind === 'extra_context') Object.assign(receipt, { question_reference: { raw_context: 'must stay local' } })
    if (kind === 'oversize') receipt.transcript = 'x'.repeat(131072)
    if (kind === 'provenance') receipt.onset_provenance = 'physical_verified'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(receipt))))
    await expect(readNativeRealtimeInputSource(command, runtime, sign)).rejects.toThrow('NATIVE_REALTIME_INPUT_UNCONFIRMED')
  })
  it.each(['http://example.test', 'https://localhost', 'http://user:secret@localhost', 'http://localhost/path'])('does not send any proof to %s', async localApiUrl => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    await expect(readNativeRealtimeInputSource(command, { ...runtime, localApiUrl }, sign)).rejects.toThrow('LOCAL_ONLY')
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('does not retry or surface private failures', async () => {
    const fetcher = vi.fn().mockRejectedValue(Error('private-synthetic-text'))
    vi.stubGlobal('fetch', fetcher)
    await expect(readNativeRealtimeInputSource(command, runtime, sign)).rejects.toThrow('NATIVE_REALTIME_INPUT_UNCONFIRMED')
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
