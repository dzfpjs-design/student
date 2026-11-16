// @vitest-environment node
import WebSocket, { WebSocketServer } from 'ws'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'
import type { QwenRealtimeConfig } from './qwen-realtime'
import type { NativeRealtimeInputSourceCommand, NativeRealtimeInputSource } from '../shared/nativeRealtimeInputSource'

vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }))
import { QwenRuntimeOutputConnection, connectQwenRuntimeOutput } from './qwen-runtime-output-connection'

const identity = { session_id: 'synthetic_session', child_id: 'synthetic_child', agent_turn_id: 'synthetic_turn',
  connection_id: 'synthetic_connection', call_id: 'synthetic_call', voice: 'Tina', permit_id: 'synthetic_permit',
  storage_generation: 'synthetic_generation' }
const inputScope = { session_id: identity.session_id, child_id: identity.child_id,
  connection_id: identity.connection_id, storage_generation: identity.storage_generation,
  client_action_id: 'synthetic_input_action', voice_input_id: 'synthetic_voice_input' }
const inputSource = () => ({ scope: { ...inputScope }, status: 'completed' as const,
  transcript: '合成测试，第二句话也要保留。', source_hash: 'a'.repeat(64), source_kind: 'realtime_transcript' as const,
  onset_provenance: 'client_reported' as const, local_only: true as const, cloud_transmission_allowed: false as const,
  arrival_status: 'not_dispatched' as const,
  replay_allowed: false as const, mastery_update_allowed: false as const })
const session = () => ({ type: 'session.updated', session: { id: 'sess_synthetic_remote', model: 'qwen3.8-omni-flash-realtime', turn_detection: null,
  audio: { output: { voice: 'Tina', format: { type: 'pcm', sample_rate: 24000 } } } } })
const tool = () => ({ type: 'response.done', response: { id: 'resp_synthetic_tool', status: 'completed', output: [
  { type: 'function_call', name: 'handle_child_turn', call_id: 'synthetic_call', status: 'completed', arguments: '{"utterance":"合成测试"}' },
] } })
const created = () => ({ type: 'response.created', response: { id: 'resp_synthetic_output' } })
const delta = () => ({ type: 'response.audio.delta', event_id: 'event_synthetic_delta', response_id: 'resp_synthetic_output',
  item_id: 'item_synthetic_audio', output_index: 0, content_index: 0, delta: Buffer.alloc(4800).toString('base64') })
const doneAudio = () => ({ ...delta(), type: 'response.audio.done', event_id: 'event_synthetic_audio_done', delta: undefined })
const done = () => ({ type: 'response.done', event_id: 'event_synthetic_response_done', response: {
  id: 'resp_synthetic_output', voice: 'Tina', status: 'completed', output: [{ id: 'item_synthetic_audio', type: 'message',
    role: 'assistant', status: 'completed', content: [{ type: 'audio' }] }],
} })
const owned: Array<() => Promise<void>> = []
afterEach(async () => { for (const close of owned.splice(0).reverse()) await close() })

async function setup(options: { handshake?: Record<string, unknown>; reply?: boolean } = {}) {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0, perMessageDeflate: false })
  await new Promise<void>(resolve => server.once('listening', resolve))
  let peer!: WebSocket, current = true
  const sent: Array<Record<string, any>> = []
  const connected = new Promise<void>(resolve => server.once('connection', ws => {
    peer = ws
    ws.on('message', raw => {
      const event = JSON.parse(raw.toString()); sent.push(event)
      if (event.type === 'session.update' && options.reply !== false) peer.send(JSON.stringify(options.handshake ?? session()))
    })
    ws.send(JSON.stringify({ type: 'session.created', session: { id: 'sess_synthetic_remote' } })); resolve()
  }))
  const socket = new WebSocket(`ws://127.0.0.1:${(server.address() as AddressInfo).port}`)
  const authority = vi.fn(async (command: NativeRealtimeOutputCommand): Promise<Record<string, unknown>> => {
    if (command.operation === 'reserve') return { state: 'reserved', response_text: '合成的原Runtime回复。', local_only: false, cloud_transmission_allowed: true,
      input_source: { client_action_id: 'realtime-' + inputScope.voice_input_id, voice_input_id: inputScope.voice_input_id } }
    if (command.operation === 'retire') return { state: 'retired', dispatch_allowed: false }
    return { state: 'bound', response_id: command.response_id, dispatch_allowed: command.operation === 'bind' }
  })
  const resolveOriginalTool = vi.fn(async (_call: { callId: string; originalInput: NativeRealtimeInputSource }) => ({ ...identity })), emit = vi.fn(), onFailure = vi.fn()
  const readOriginalInput = vi.fn(async (_command: NativeRealtimeInputSourceCommand): Promise<NativeRealtimeInputSource> => inputSource())
  const connection = new QwenRuntimeOutputConnection(socket, { connectionId: identity.connection_id, voice: 'Tina', authority,
    resolveOriginalTool, readOriginalInput, emit, onFailure, isCurrent: () => current })
  owned.push(async () => {
    await connection.close()
    for (const client of server.clients) client.terminate()
    await new Promise<void>(resolve => server.close(() => resolve()))
  })
  await connected
  return { connection, socket, peer, sent, authority, resolveOriginalTool, readOriginalInput, emit, onFailure, retire: () => { current = false },
    send: (event: unknown) => peer.send(JSON.stringify(event)),
    rawStart: () => { peer.send(JSON.stringify({ type: 'response.created', response: { id: 'resp_synthetic_tool' } })); peer.send(JSON.stringify(tool())) },
    start: async () => { await connection.expectOriginalInput(inputScope)
      peer.send(JSON.stringify({ type: 'response.created', response: { id: 'resp_synthetic_tool' } })); peer.send(JSON.stringify(tool())) } }
}

describe('main same-socket Runtime output registration', () => {
  it('does not execute an unsolicited tool even when the supplier invents a complete response', async () => {
    const c = await setup(); await c.connection.ready; c.rawStart()
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_RESPONSE_UNEXPECTED'))
    expect(c.resolveOriginalTool).not.toHaveBeenCalled(); expect(c.authority).not.toHaveBeenCalled()
  })
  it('does not overwrite the registered original with a second input while waiting or executing', async () => {
    const c = await setup(); await c.connection.ready
    await c.connection.expectOriginalInput(inputScope)
    await expect(c.connection.expectOriginalInput({ ...inputScope, voice_input_id: 'another_voice_input' })).rejects.toThrow('INPUT_BUSY')
    c.rawStart()
    await vi.waitFor(() => expect(c.resolveOriginalTool).toHaveBeenCalledTimes(1))
    expect(c.resolveOriginalTool.mock.calls[0][0].originalInput.transcript).toBe(inputSource().transcript)
    expect(c.onFailure).not.toHaveBeenCalled()
  })
  it('does not speak a different completed Runtime turn from the same child and session', async () => {
    const c = await setup(); await c.connection.ready
    c.authority.mockResolvedValueOnce({ state: 'reserved', response_text: '另一个原回合的回复', local_only: false, cloud_transmission_allowed: true,
      input_source: { client_action_id: 'realtime-another_voice_input', voice_input_id: 'another_voice_input' } })
    await c.start()
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_OUTPUT_UNCONFIRMED'))
    expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(0)
    expect(c.emit).not.toHaveBeenCalled()
    expect(c.authority.mock.calls.at(-1)?.[0].operation).toBe('retire')
  })
  it('does not reuse a prior supplier audio response in a later captured turn', async () => {
    const c = await setup(); await c.connection.ready; await c.start()
    await vi.waitFor(() => expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1))
    c.send(created()); c.send(delta()); c.send(doneAudio()); c.send(done())
    await vi.waitFor(() => expect(c.authority.mock.calls.at(-1)?.[0].operation).toBe('retire'))
    const nextScope = { ...inputScope, voice_input_id: 'next_voice_input', client_action_id: 'next_capture_action' }
    c.readOriginalInput.mockImplementation(async () => ({ ...inputSource(), scope: nextScope }))
    c.resolveOriginalTool.mockResolvedValue({ ...identity, call_id: 'next_synthetic_call', agent_turn_id: 'next_synthetic_turn' })
    c.authority.mockImplementation(async command => command.operation === 'reserve'
      ? { state: 'reserved', response_text: '下一句合成回复', local_only: false, cloud_transmission_allowed: true,
        input_source: { client_action_id: 'realtime-next_voice_input', voice_input_id: 'next_voice_input' } }
      : { state: command.operation === 'retire' ? 'retired' : 'bound', dispatch_allowed: command.operation === 'bind', response_id: command.response_id })
    await c.connection.expectOriginalInput(nextScope)
    c.send({ type: 'response.created', response: { id: 'next_synthetic_tool_response' } })
    const nextTool = tool(); nextTool.response.id = 'next_synthetic_tool_response'; nextTool.response.output[0].call_id = 'next_synthetic_call'
    c.send(nextTool)
    await vi.waitFor(() => expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(2))
    c.send(created()); c.send(delta())
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_RESPONSE_REPLAY'))
    expect(c.emit).toHaveBeenCalledTimes(3)
  })
  it.each(['hash', 'transcript', 'permission', 'error', 'another_engine_dispatched'])('does not run the tool when the local original changed (%s)', async mutation => {
    const c = await setup(); await c.connection.ready
    await c.connection.expectOriginalInput(inputScope)
    const changed = inputSource()
    if (mutation === 'hash') changed.source_hash = 'b'.repeat(64)
    if (mutation === 'transcript') changed.transcript = '被改写的原话'
    if (mutation === 'permission') Object.assign(changed, { replay_allowed: true })
    if (mutation === 'another_engine_dispatched') Object.assign(changed, { arrival_status: 'current' })
    if (mutation === 'error') c.readOriginalInput.mockRejectedValueOnce(Error('synthetic-private-details'))
    else c.readOriginalInput.mockResolvedValueOnce(changed)
    c.rawStart()
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledTimes(1))
    expect(c.resolveOriginalTool).not.toHaveBeenCalled(); expect(c.authority).not.toHaveBeenCalled()
    expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(0)
    expect(JSON.stringify(c.onFailure.mock.calls)).not.toContain('synthetic-private-details')
  })
  it.each(['current', 'superseded'] as const)('local readback (%s) cannot authorize a new supplier tool for an old arrival', async arrival_status => {
    const c = await setup(); await c.connection.ready
    c.readOriginalInput.mockResolvedValueOnce({ ...inputSource(), arrival_status })
    await expect(c.connection.expectOriginalInput(inputScope)).rejects.toThrow('INPUT_UNCONFIRMED')
    expect(c.resolveOriginalTool).not.toHaveBeenCalled(); expect(c.authority).not.toHaveBeenCalled()
    expect(c.sent).toHaveLength(1)
  })
  it('does not revive after cancellation while reading the original source', async () => {
    const c = await setup(); await c.connection.ready
    let resolve!: (value: ReturnType<typeof inputSource>) => void
    c.readOriginalInput.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    const waiting = c.connection.expectOriginalInput(inputScope)
    const rejection = expect(waiting).rejects.toThrow('INPUT_UNCONFIRMED')
    await c.connection.close(); await rejection; resolve(inputSource())
    expect(c.resolveOriginalTool).not.toHaveBeenCalled(); expect(c.onFailure).toHaveBeenCalledTimes(1)
  })
  it('uses only this real socket session and tool terminal, then dispatches original text and ordered PCM once', async () => {
    const c = await setup(); await c.connection.ready
    expect(c.sent[0].session.turn_detection).toBeNull()
    expect(c.sent[0].session.audio.output).toEqual({ voice: 'Tina', format: { type: 'pcm', sample_rate: 24000 } })
    await c.start()
    await vi.waitFor(() => expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1))
    expect(c.resolveOriginalTool).toHaveBeenCalledExactlyOnceWith({ callId: 'synthetic_call', originalInput: inputSource() })
    expect(JSON.parse(c.sent.find(e => e.type === 'conversation.item.create')!.item.output)).toEqual({ response_text: '合成的原Runtime回复。' })
    c.send(created()); c.send(delta()); c.send(doneAudio()); c.send(done())
    await vi.waitFor(() => expect(c.authority.mock.calls.at(-1)?.[0].operation).toBe('retire'))
    expect(c.emit.mock.calls.map(([event]) => event.sequence)).toEqual([1, 2, 3])
    expect(c.onFailure).not.toHaveBeenCalled()
    expect(c.socket.readyState).toBe(WebSocket.OPEN)
  })

  it.each(['automatic_response', 'changed_voice', 'changed_rate'])('does not grant output for handshake %s', async kind => {
    const handshake = session()
    if (kind === 'automatic_response') Object.assign(handshake.session, { turn_detection: { type: 'server_vad' } })
    if (kind === 'changed_voice') handshake.session.audio.output.voice = 'OtherVoice'
    if (kind === 'changed_rate') handshake.session.audio.output.format.sample_rate = 16000
    const c = await setup({ handshake })
    await expect(c.connection.ready).rejects.toThrow('SESSION_UNCONFIRMED')
    expect(c.authority).not.toHaveBeenCalled(); expect(c.resolveOriginalTool).not.toHaveBeenCalled()
  })

  it('retires a changed session after ready instead of trusting prior confirmation', async () => {
    const c = await setup(); await c.connection.ready; c.send(session())
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_SESSION_CHANGED'))
    expect(c.authority).not.toHaveBeenCalled()
  })

  it('qualifies the official explicit-null ack omission only for the same pending update, model and session', async () => {
    const handshake = session()
    delete (handshake.session as { turn_detection?: null }).turn_detection
    const c = await setup({ handshake }); await c.connection.ready; await c.start()
    await vi.waitFor(() => expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1))
    expect(c.sent[0].session.turn_detection).toBeNull()
    for (const field of ['model', 'id'] as const) {
      const invalid = session()
      delete (invalid.session as { turn_detection?: null }).turn_detection
      invalid.session[field] = 'unrelated_binding'
      const denied = await setup({ handshake: invalid })
      await expect(denied.connection.ready).rejects.toThrow('SESSION_UNCONFIRMED')
      expect(denied.authority).not.toHaveBeenCalled()
    }
  })

  it('rejects a tool terminal whose response was never received on this connection', async () => {
    const c = await setup(); await c.connection.ready; c.send(tool())
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_TOOL_UNCONFIRMED'))
    expect(c.resolveOriginalTool).not.toHaveBeenCalled(); expect(c.authority).not.toHaveBeenCalled()
  })

  it('does not export Runtime text for a rewritten connection, voice or call binding', async () => {
    for (const key of ['connection_id', 'call_id', 'voice', 'child_id', 'session_id', 'storage_generation'] as const) {
      const c = await setup(); await c.connection.ready
      c.resolveOriginalTool.mockResolvedValueOnce({ ...identity, [key]: 'different_binding' }); await c.start()
      await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_RUNTIME_UNCONFIRMED'))
      expect(c.authority).not.toHaveBeenCalled(); expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(0)
    }
  })

  it('does not resume after ownership retires during Runtime resolution', async () => {
    const c = await setup(); await c.connection.ready
    let resolve!: (value: typeof identity) => void
    c.resolveOriginalTool.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    await c.start(); await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
    c.retire(); resolve({ ...identity })
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_RUNTIME_UNCONFIRMED'))
    expect(c.authority).not.toHaveBeenCalled()
  })

  it('does not publish frames or retry after a committed binding loses its receipt', async () => {
    const c = await setup(); await c.connection.ready; await c.start()
    await vi.waitFor(() => expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1))
    c.authority.mockRejectedValueOnce(Error('synthetic-private-error')); c.send(created()); c.send(delta())
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_OUTPUT_UNCONFIRMED'))
    expect(c.emit).not.toHaveBeenCalled()
    expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1)
    expect(JSON.stringify(c.onFailure.mock.calls)).not.toContain('synthetic-private-error')
  })

  it.each(['audio', 'binary', 'invalid_json'])('closes unauthorized %s without Runtime execution', async kind => {
    const c = await setup(); await c.connection.ready
    if (kind === 'audio') c.send(delta())
    if (kind === 'binary') c.peer.send(Buffer.from('invalid'))
    if (kind === 'invalid_json') c.peer.send('{invalid')
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledTimes(1))
    expect(c.resolveOriginalTool).not.toHaveBeenCalled(); expect(c.emit).not.toHaveBeenCalled()
  })

  it('rejects replay of the same consumed tool call after its normal output', async () => {
    const c = await setup(); await c.connection.ready; await c.start()
    await vi.waitFor(() => expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1))
    c.send(created()); c.send(delta()); c.send(doneAudio()); c.send(done())
    await vi.waitFor(() => expect(c.emit).toHaveBeenCalledTimes(3)); await new Promise(resolve => setImmediate(resolve))
    await expect(c.start()).rejects.toThrow('INPUT_REPLAY')
    c.rawStart()
    await vi.waitFor(() => expect(c.onFailure).toHaveBeenCalledExactlyOnceWith('QWEN_CONNECTION_RESPONSE_REPLAY'))
    expect(c.resolveOriginalTool).toHaveBeenCalledTimes(1)
    expect(c.sent.filter(e => e.type === 'response.create')).toHaveLength(1)
  })

  it('closes the socket immediately and denies a late resolution after explicit cancellation', async () => {
    const c = await setup(); await c.connection.ready
    let resolve!: (value: typeof identity) => void
    c.resolveOriginalTool.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    await c.start(); await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
    await c.connection.close(); resolve({ ...identity }); await new Promise(resolve => setImmediate(resolve))
    expect(c.socket.readyState).not.toBe(WebSocket.OPEN)
    expect(c.authority).not.toHaveBeenCalled(); expect(c.onFailure).toHaveBeenCalledTimes(1)
  })

  it('requires enabled private selected configuration before opening any supplier socket', () => {
    const owner = { connectionId: identity.connection_id, authority: vi.fn(), resolveOriginalTool: vi.fn(), readOriginalInput: vi.fn(), emit: vi.fn(), onFailure: vi.fn(), isCurrent: () => true }
    for (const config of [null, { apiKey: 'synthetic-secret-value', endpoint: 'https://voice.example.test', model: 'other', voice: 'Tina', enabled: true },
      { apiKey: 'synthetic-secret-value', endpoint: 'https://ws-test.cn-beijing.maas.aliyuncs.com', model: 'qwen3.8-omni-flash-realtime', voice: 'Tina', enabled: false }]) {
      expect(() => connectQwenRuntimeOutput({ ...owner, currentConfig: () => config as QwenRealtimeConfig | null })).toThrow('CONFIG_UNAVAILABLE')
    }
  })
})
