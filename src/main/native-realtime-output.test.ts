import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHmac } from 'node:crypto'
import { executeNativeRealtimeOutput, signNativeRealtimeOutput } from './native-realtime-output'
import { nativeRealtimeOutputFields, nativeRealtimeOutputScopeFields, type NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'
const command: NativeRealtimeOutputCommand = { operation: 'reserve', session_id: 'synthetic_session', child_id: 'synthetic_child',
  agent_turn_id: 'synthetic_turn', connection_id: 'synthetic_connection', call_id: 'synthetic_call', voice: 'Tina',
  permit_id: 'synthetic_permit', storage_generation: 'synthetic_generation', response_id: '', issued_at: '2026-10-05T18:00:00.000Z' }
describe('main-only realtime output proof', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('uses an independent purpose and signs every scope field in an explicit cross-language order', () => {
    const expected = createHmac('sha256', Buffer.from('ab'.repeat(32), 'hex'))
      .update(JSON.stringify(['native_realtime_output_v1', ...nativeRealtimeOutputFields.map(f => command[f])])).digest('hex')
    expect(signNativeRealtimeOutput('ab'.repeat(32), command)).toBe(expected)
  })
  it.each(nativeRealtimeOutputFields)('changing %s invalidates the original proof', field => {
    const changed = { ...command, [field]: 'different_value' } as NativeRealtimeOutputCommand
    expect(signNativeRealtimeOutput('ab'.repeat(32), changed)).not.toBe(signNativeRealtimeOutput('ab'.repeat(32), command))
  })
  it.each([undefined, '', 'aa', 'ZZ'.repeat(32)])('rejects missing or malformed native authority', key => {
    expect(() => signNativeRealtimeOutput(key, command)).toThrow('NATIVE_REALTIME_AUTHORITY_UNAVAILABLE')
  })
  const runtime = { status: 'ready', localApiUrl: 'http://127.0.0.1:12345', sessionToken: 'synthetic-token' }
  const scope = Object.fromEntries(nativeRealtimeOutputScopeFields.map(f => [f, command[f]]))
  const sign = (body: NativeRealtimeOutputCommand) => signNativeRealtimeOutput('ab'.repeat(32), body)
  it('fetches only the local source and never retries a missing post-commit receipt', async () => {
    const fetcher = vi.fn().mockRejectedValue(Error('Synthetic private error'))
    vi.stubGlobal('fetch', fetcher)
    await expect(executeNativeRealtimeOutput(command, runtime, sign)).rejects.toThrow('NATIVE_REALTIME_RECEIPT_UNCONFIRMED')
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0][0]).toContain('/voice-output-authority')
    const body = JSON.parse(fetcher.mock.calls[0][1].body)
    expect('response_text' in body).toBe(false)
    expect('cloud_transmission_allowed' in body).toBe(false)
  })
  it.each(['https://127.0.0.1', 'http://example.test', 'http://127.0.0.1/remote', 'http://user:secret@localhost'])('does not post to an unowned URL (%s)', async localApiUrl => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    await expect(executeNativeRealtimeOutput(command, { ...runtime, localApiUrl }, sign)).rejects.toThrow('NATIVE_REALTIME_LOCAL_ONLY')
    expect(fetcher).not.toHaveBeenCalled()
  })
  it.each(['wrong_child', 'wrong_generation', 'wrong_response', 'completion_true', 'readback_dispatch', 'oversize'])('rejects an unsafe response (%s)', async mode => {
    const request: NativeRealtimeOutputCommand = { ...command, operation: mode === 'readback_dispatch' ? 'inspect' : 'bind', response_id: 'resp_synthetic_output' }
    const body = { state: 'bound', scope: { ...scope }, response_id: request.response_id,
      dispatch_allowed: true, generation_completed: false, rendered_completed: false, ignored: '' }
    if (mode === 'wrong_child') body.scope.child_id = 'different_child'
    if (mode === 'wrong_generation') body.scope.storage_generation = 'different_generation'
    if (mode === 'wrong_response') body.response_id = 'different_response'
    if (mode === 'completion_true') Object.assign(body, { rendered_completed: true })
    if (mode === 'oversize') body.ignored = 'x'.repeat(8192)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body))))
    await expect(executeNativeRealtimeOutput(request, runtime, sign)).rejects.toThrow('NATIVE_REALTIME_RECEIPT_UNCONFIRMED')
  })
  it.each(['generation_completed', 'rendered_completed'])('does not accept a reserve claiming %s', async field => {
    const body = { state: 'reserved', scope, local_only: false, cloud_transmission_allowed: true,
      response_text: '合成回复。', generation_completed: false, rendered_completed: false, [field]: true }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body))))
    await expect(executeNativeRealtimeOutput(command, runtime, sign)).rejects.toThrow('NATIVE_REALTIME_RECEIPT_UNCONFIRMED')
  })
})
