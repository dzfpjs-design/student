import { EventEmitter, once } from 'node:events'
import { createServer } from 'node:http'
import { PassThrough } from 'node:stream'
import type WebSocket from 'ws'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
const money = vi.hoisted(() => ({ deny: vi.fn() }))
vi.mock('./money-egress', () => ({ denyUnpricedMoneyEgress: money.deny }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }))
vi.mock('ws', () => ({ default: class { static OPEN = 1; constructor() { throw Error('NO_NETWORK_ALLOWED') } } }))
import { QwenNativeSocketRelay } from './qwen-native-socket'
import { sanitizeDiagnosticValue } from './diagnostics'
import { NativeRealtimeCapabilityGateway } from './native-realtime-capabilities'
import type { QwenNativeSocketEvent } from '../shared/qwenNativeSocket'

class Socket extends EventEmitter {
  readyState = 0; bufferedAmount = 0; sends: string[] = []; ended = 0; holdSend = false
  sent: ((error?: Error) => void) | null = null
  pings: Buffer[] = []; pingCallback: ((error?: Error) => void) | null = null
  ping(data: Buffer, _masked: boolean, callback: (error?: Error) => void): void { this.pings.push(Buffer.from(data)); this.pingCallback = callback; queueMicrotask(() => callback()) }
  pong(data = this.pings.at(-1) ?? Buffer.alloc(0)): void { this.emit('pong', data) }
  open(): void { this.readyState = 1; this.emit('open') }
  send(raw: string, _options: object, callback: (error?: Error) => void): void {
    this.sends.push(raw); this.sent = callback
    if (!this.holdSend) queueMicrotask(() => callback())
  }
  terminate(): void { this.ended++; this.readyState = 3; this.emit('close', 1006, Buffer.from('private provider detail')) }
  message(value: unknown, binary = false): void { this.emit('message', Buffer.from(typeof value === 'string' ? value : JSON.stringify(value)), binary) }
}
const event = { sender: { id: 7 } }, wrong = { sender: { id: 8 } }
const config = { apiKey: 'public-synthetic-key-not-a-real-credential', endpoint: 'https://public-workspace.cn-beijing.maas.aliyuncs.com',
  model: 'qwen3.8-omni-flash-realtime' as const, voice: 'Tina', enabled: true }
function fixture(autoOpen = true, onShape?: () => void) {
  const state = { allowed: true, owner: true, configured: true, epoch: 3, serial: 0 }
  const ids = new Set<string>(), sockets: Socket[] = [], output: QwenNativeSocketEvent[] = []
  const failures: Array<[string, string]> = []
  const retirements: string[] = []
  const payloadFailures: string[] = []
  const shapes: unknown[] = []
  const postOpenShapes: unknown[] = []
  const establishedEnds = vi.fn()
  const policy = vi.fn(async () => ({ capture: state.configured ? 'configured' : 'bounded_segmented', runtimeEpoch: state.epoch }))
  const load = vi.fn(async () => config)
  const retire = vi.fn((_event: typeof event, id: string) => { ids.delete(id) })
  const dial = vi.fn((_url: string, _options: object) => {
    const socket = new Socket(); sockets.push(socket)
    if (autoOpen) queueMicrotask(() => { socket.open(); socket.message({ type: 'session.created', session: { id: 'public-provider-session' } }) })
    return socket as unknown as WebSocket
  })
  const relay = new QwenNativeSocketRelay({ allowed: e => state.allowed && e.sender.id === 7,
    current: (_e, id, sid) => state.owner && ids.has(id) && sid === 'session-public',
    register: () => { ids.clear(); const id = 'host-' + ++state.serial; ids.add(id); return id },
    retire, policy, config: load, emit: (_e, value) => { output.push(value) },
    onOpenFailure: (stage, failure, retirement, payloadFailure, shape) => { failures.push([stage, failure]); retirements.push(retirement); payloadFailures.push(payloadFailure); shapes.push(shape) },
    onMessageShapeFailure: shape => { postOpenShapes.push(shape); onShape?.() }, onEstablishedEnd: establishedEnds, dial })
  return { relay, state, ids, sockets, output, policy, load, retire, dial, failures, retirements, payloadFailures, shapes, postOpenShapes, establishedEnds }
}
beforeEach(() => { vi.useFakeTimers(); money.deny.mockReset() })
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers() })
describe('Main-owned native Qwen socket', () => {
  it.each([
    ['InvalidApiKey', 'secret', 'credential', 'string', 'string'],
    ['AccessDenied', 'secret', 'access', 'string', 'string'],
    ['Arrearage', 'secret', 'balance', 'string', 'string'],
    ['Throttling.RateQuota', 'secret', 'rate_limit', 'string', 'string'],
    ['Throttling.AllocationQuota', 'secret', 'quota', 'string', 'string'],
    ['ModelNotFound', 'secret', 'model_not_found', 'string', 'string'],
    ['NotFound', 'secret', 'resource_not_found', 'string', 'string'],
    ['AccessDenied.Unpurchased', 'secret', 'access', 'string', 'string'],
    ['InvalidParameter', 'secret', 'invalid_parameter', 'string', 'string'],
    ['insufficient_quota', 'secret', 'quota', 'string', 'string'],
    ['InternalError', 'secret', 'provider_unavailable', 'string', 'string'],
    ['ServiceUnavailable', 'secret', 'provider_unavailable', 'string', 'string'],
    ['PRIVATE_DYNAMIC_CODE', 'PRIVATE_CONTENT', 'unknown', 'string', 'string'],
    [['PRIVATE'], null, 'unknown', 'array', 'null'],
    [1, false, 'unknown', 'number', 'boolean'],
  ])('records bounded untyped provider shape %#', async (code, message, category, codeType, messageType) => {
    const f = fixture(false); let calls = 0
    f.policy.mockImplementation(async () => ++calls >= 3 ? await new Promise<never>(() => {}) : { capture: 'configured', runtimeEpoch: 3 })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(0); f.sockets[0].open(); await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].message({ code, message }); await rejected
    expect(f.shapes[0]).toMatchObject({ socket_message_code_type: codeType, socket_message_message_type: messageType, socket_provider_category: category })
    expect(JSON.stringify([f.output, f.shapes, f.failures])).not.toContain('PRIVATE_')
    expect(f.sockets[0].ended).toBe(1)
  })
  it('records one safe shape after open and ignores later messages', async () => {
    const f=fixture(); await f.relay.open(event,'session-public'); f.output.length=0
    f.sockets[0].message({code:'PRIVATE_DYNAMIC',message:'PRIVATE_DETAIL'})
    f.sockets[0].message({code:'PRIVATE_LATE',message:'PRIVATE_DETAIL'})
    expect(f.postOpenShapes).toHaveLength(1)
    expect(f.postOpenShapes[0]).toMatchObject({socket_message_code_type:'string',socket_message_message_type:'string',socket_provider_category:'unknown'})
    expect(JSON.stringify([f.postOpenShapes,f.output])).not.toContain('PRIVATE_')
    expect(f.sockets[0].ended).toBe(1)
  })
  it('records at most one shape even when diagnostic dispatch reenters the socket', async () => {
    let f!: ReturnType<typeof fixture>
    f=fixture(true,()=>f.sockets[0].message({code:'PRIVATE_REENTERED',message:'PRIVATE_CONTENT'}))
    await f.relay.open(event,'session-public')
    f.sockets[0].message({code:'PRIVATE_FIRST',message:'PRIVATE_CONTENT'})
    expect(f.postOpenShapes).toHaveLength(1)
    expect(JSON.stringify(f.postOpenShapes)).not.toContain('PRIVATE_')
    expect(f.sockets[0].ended).toBe(1)
  })
  it.each(['owner', 'policy'] as const)('never records a message after %s authority has expired', async failure => {
    const f=fixture(); await f.relay.open(event,'session-public')
    if (failure==='owner') f.state.owner=false
    else { f.state.configured=false; await vi.advanceTimersByTimeAsync(500) }
    f.sockets[0].message({code:'PRIVATE_DYNAMIC',message:'PRIVATE_DETAIL'})
    expect(f.postOpenShapes).toEqual([])
  })
  it.each([
    ['InvalidApiKey', 'QWEN_NATIVE_CREDENTIAL_REJECTED'],
    ['invalid_api_key', 'QWEN_NATIVE_CREDENTIAL_REJECTED'],
    ['AccessDenied', 'QWEN_NATIVE_ACCESS_DENIED'],
    ['access_denied', 'QWEN_NATIVE_ACCESS_DENIED'],
    ['Arrearage', 'QWEN_NATIVE_ACCOUNT_BALANCE'],
  ])('safely conveys documented provider code %s across a pending open', async (providerCode, expected) => {
    const f = fixture(false); let calls = 0
    f.policy.mockImplementation(async () => ++calls >= 3 ? await new Promise<never>(() => {}) : { capture: 'configured', runtimeEpoch: 3 })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow(expected)
    await vi.advanceTimersByTimeAsync(0); f.sockets[0].open(); await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].message({ code: providerCode, message: 'PRIVATE_PROVIDER_DETAIL' })
    await rejected
    expect(f.retirements).toEqual(['provider_error'])
    expect(f.output).toEqual([{ hostConnectionId: 'host-1', closed: true, error: expected }])
    expect(f.sockets[0].ended).toBe(1)
    expect(JSON.stringify([f.output, f.failures, f.shapes])).not.toContain('PRIVATE_PROVIDER_DETAIL')
  })
  it.each([
    { code: 'PRIVATE_UNKNOWN_CODE', message: 'PRIVATE_DETAIL' },
    { code: 'InvalidApiKey', message: null },
    { code: 'InvalidApiKey', message: 'PRIVATE_DETAIL', type: null },
    { code: ['InvalidApiKey'], message: 'PRIVATE_DETAIL' },
    { code: 'InvalidApiKey', type: 'session.created', message: 'PRIVATE_DETAIL' },
  ])('does not classify malformed or unknown untyped errors %#', async value => {
    const f = fixture(), opened = await f.relay.open(event, 'session-public')
    f.output.length = 0
    f.sockets[0].message(value)
    expect(f.output).toEqual([{ hostConnectionId: opened.nativeHostConnectionId, closed: true, error: 'QWEN_NATIVE_SOCKET_PAYLOAD_INVALID' }])
    expect(JSON.stringify(f.output)).not.toContain('PRIVATE_')
  })
  it('sanitizes a known typed provider error after open', async () => {
    const f = fixture(); await f.relay.open(event, 'session-public'); f.output.length = 0
    f.sockets[0].message({ type: 'error', error: { code: 'InvalidApiKey', message: 'PRIVATE_DETAIL' } })
    expect(f.output).toEqual([{ hostConnectionId: 'host-1', closed: true, error: 'QWEN_NATIVE_CREDENTIAL_REJECTED' }])
    expect(JSON.stringify(f.output)).not.toContain('PRIVATE_DETAIL')
  })
  it.each([
    ['message_binary', Buffer.from('{}'), true],
    ['message_oversize', Buffer.alloc(2_097_153), false],
    ['message_json', Buffer.from('PRIVATE_INVALID_JSON'), false],
    ['message_structure', Buffer.from('{"private":"PRIVATE_CONTENT"}'), false],
    ['message_conversion', {} as any, false],
  ])('classifies %s during post-open without exposing message contents', async (expected, data, binary) => {
    const f = fixture(false); let calls = 0
    f.policy.mockImplementation(async () => ++calls >= 3 ? await new Promise<never>(() => {}) : { capture: 'configured', runtimeEpoch: 3 })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0); f.sockets[0].open(); await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].emit('message', data, binary); await rejected
    expect(f.failures[0][0]).toBe('post_open_policy')
    expect(f.retirements).toEqual(['invalid_payload']); expect(f.payloadFailures).toEqual([expected])
    expect(JSON.stringify(f.payloadFailures)).not.toContain('PRIVATE_')
  })
  it.each([
    [null, 'null', 'not_object', 'not_object', 'not_object', 'not_object'],
    [[], 'array', 'not_object', 'not_object', 'not_object', 'not_object'],
    [5, 'scalar', 'not_object', 'not_object', 'not_object', 'not_object'],
    [{header:{secret:'synthetic-30e5e191af862c2229b928ba'}, payload:{}, error:'synthetic-30e5e191af862c2229b928ba'}, 'object', 'missing', 'object', 'object', 'other'],
    [{type:17, code:'synthetic-30e5e191af862c2229b928ba', message:'synthetic-30e5e191af862c2229b928ba', event:'synthetic-30e5e191af862c2229b928ba'}, 'object', 'non_string', 'missing', 'missing', 'missing'],
  ])('records only bounded structure for malformed provider event %#', async (value, top, typ, header, payload, err) => {
    const f = fixture(false); let calls = 0
    f.policy.mockImplementation(async () => ++calls >= 3 ? await new Promise<never>(() => {}) : { capture: 'configured', runtimeEpoch: 3 })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0); f.sockets[0].open(); await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].message(value); f.sockets[0].message({header:'SECOND'}); await rejected
    expect(f.shapes).toHaveLength(1)
    expect(f.shapes[0]).toMatchObject({socket_message_top: top, socket_message_type: typ, socket_message_header: header, socket_message_payload: payload, socket_message_error: err})
    if (value && !Array.isArray(value) && typeof value === 'object' && 'code' in value) expect(f.shapes[0]).toMatchObject({socket_message_code: 'other', socket_message_message: 'other', socket_message_event: 'other'})
    expect(JSON.stringify(f.shapes)).not.toContain('PRIVATE_')
  })
  it('distinguishes early send and retains its first retirement reason', async () => {
    const f = fixture(false); let calls = 0
    f.policy.mockImplementation(async () => ++calls >= 3 ? await new Promise<never>(() => {}) : { capture: 'configured', runtimeEpoch: 3 })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0); f.sockets[0].open(); await vi.advanceTimersByTimeAsync(0)
    await expect(f.relay.send(event, 'host-1', '{}')).rejects.toThrow(); await rejected
    expect(f.retirements).toEqual(['invalid_payload']); expect(f.payloadFailures).toEqual(['send_not_opened'])
  })
  it('reports the first provider retirement during post-open policy without leaking the provider payload', async () => {
    const f = fixture(false)
    let policyCalls = 0
    f.policy.mockImplementation(async () => {
      if (++policyCalls >= 3) return await new Promise<never>(() => {})
      return { capture: 'configured', runtimeEpoch: 3 }
    })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].open()
    await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].message({ type: 'error', error: { message: 'PRIVATE_PROVIDER_DETAIL' } })
    await rejected
    expect(f.failures[0][0]).toBe('post_open_policy')
    expect(f.retirements).toEqual(['provider_error'])
    expect(JSON.stringify([f.failures, f.retirements])).not.toContain('PRIVATE_')
  })
  it('distinguishes a failed post-open policy refresh from a provider error', async () => {
    const f = fixture(false)
    let policyCalls = 0
    f.policy.mockImplementation(async () => {
      if (++policyCalls >= 3) throw new Error('PRIVATE_POLICY_DETAIL')
      return { capture: 'configured', runtimeEpoch: 3 }
    })
    const pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0)
    f.sockets[0].open()
    await rejected
    expect(f.failures[0][0]).toBe('post_open_policy')
    expect(f.retirements).toEqual(['policy_unconfirmed'])
    expect(JSON.stringify([f.failures, f.retirements])).not.toContain('PRIVATE_')
  })
  it.each([['error', 'transport_error'], ['unexpected-response', 'http_rejected'], ['close', 'socket_closed']] as const)(
    'records only fixed handshake category for %s without provider details', async (eventName, category) => {
      const f = fixture(false), pending = f.relay.open(event, 'session-public')
      const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
      await vi.advanceTimersByTimeAsync(0)
      f.sockets[0].emit(eventName, new Error('PRIVATE_URL_HEADER_EXCEPTION'))
      await rejected
      expect(f.failures).toEqual([['handshake', category]])
      expect(JSON.stringify(f.failures)).not.toContain('PRIVATE_')
    })
  it.each([
    [401, { code: 'InvalidApiKey', message: 'PRIVATE_PROVIDER_MESSAGE' }, 'credential', 'QWEN_NATIVE_CREDENTIAL_REJECTED'],
    [403, { error: { code: 'Endpoint.AccessDenied', message: 'PRIVATE_PROVIDER_MESSAGE' } }, 'access', 'QWEN_NATIVE_ACCESS_DENIED'],
    [403, { code: 'Throttling.AllocationQuota', message: 'PRIVATE_PROVIDER_MESSAGE' }, 'quota', 'QWEN_NATIVE_SOCKET_UNCONFIRMED'],
    [429, { code: 'PRIVATE_DYNAMIC_CODE', message: 'PRIVATE_PROVIDER_MESSAGE' }, 'rate_limit', 'QWEN_NATIVE_SOCKET_UNCONFIRMED'],
  ] as const)('records HTTP %s with safe category %j', async (statusCode, body, category, expected) => {
    const f = fixture(false), pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow(expected)
    await vi.advanceTimersByTimeAsync(0)
    const socket = f.sockets[0], terminate = socket.terminate.bind(socket)
    vi.spyOn(socket, 'terminate').mockImplementation(() => {
      socket.emit('error', new Error('PRIVATE_TRANSPORT_DETAIL'))
      terminate()
    })
    const response = Object.assign(new PassThrough(), { statusCode })
    socket.emit('unexpected-response', {}, response)
    response.end(JSON.stringify(body))
    await rejected
    expect(f.failures).toEqual([['handshake', 'http_rejected']])
    expect(f.shapes).toEqual([{ statusCode, socket_provider_category: category }])
    expect(JSON.stringify([f.output, f.shapes])).not.toContain('PRIVATE_')
    expect(socket.ended).toBe(1)
  })
  it.each(['oversized', 'stalled'])('keeps the HTTP status when the response is %s', async kind => {
    const f = fixture(false), pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_ACCESS_DENIED')
    await vi.advanceTimersByTimeAsync(0)
    const response = Object.assign(new PassThrough(), { statusCode: 403 })
    f.sockets[0].emit('unexpected-response', {}, response)
    if (kind === 'oversized') response.end(' '.repeat(5_000))
    else await vi.advanceTimersByTimeAsync(500)
    await rejected
    expect(f.shapes).toEqual([{ statusCode: 403, socket_provider_category: 'access' }])
    expect(response.listenerCount('data')).toBe(0)
    expect(f.sockets[0].ended).toBe(1)
    response.destroy()
  })
  it('retains only allowlisted status and category from a real WebSocket HTTP rejection', async () => {
    vi.useRealTimers()
    const { default: RealWebSocket } = await vi.importActual<typeof import('ws')>('ws')
    const server = createServer((_request, response) => {
      response.writeHead(403, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({ code: 'Endpoint.AccessDenied', message: 'PRIVATE_HTTP_DETAILS' }))
    })
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')
    const { port } = server.address() as { port: number }
    const f = fixture(false)
    f.dial.mockImplementation(() => new RealWebSocket(`ws://127.0.0.1:${port}`))
    try {
      await expect(f.relay.open(event, 'session-public')).rejects.toThrow('QWEN_NATIVE_ACCESS_DENIED')
      expect(f.failures).toEqual([['handshake', 'http_rejected']])
      expect(sanitizeDiagnosticValue(f.shapes[0])).toEqual({ statusCode: 403, socket_provider_category: 'access' })
      expect(JSON.stringify(f.output)).not.toContain('PRIVATE_')
    } finally {
      f.relay.retireOwner(event.sender.id)
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  })
  it('records a bounded handshake timeout without the provider address', async () => {
    const f = fixture(false), pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(20_001); await rejected
    expect(f.failures).toEqual([['handshake', 'timeout']])
  })
  it('preserves the real relay and gateway authority across six minutes and retires at its bounded lifetime', async () => {
    let authorityNow = 0
    const owner = { status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'synthetic', runtimeEpoch: 3, child: {}, revision: 1 }
    const gateway = new NativeRealtimeCapabilityGateway({ allowed: () => true, current: () => owner,
      policy: async () => ({ capture: 'configured', runtimeEpoch: 3 }), sign: () => 'synthetic', now: () => authorityNow })
    const socket = new Socket(), output: QwenNativeSocketEvent[] = []
    const relay = new QwenNativeSocketRelay({ allowed: () => true,
      current: (sender, id, session) => gateway.isCurrent(sender, id, session),
      register: sender => gateway.register(sender),
      confirmModel: (sender, id, session, model) => gateway.confirmSocketModel(sender, id, session, model),
      retire: (sender, id) => gateway.close(sender, id),
      policy: async () => ({ capture: 'configured', runtimeEpoch: 3 }), config: async () => config,
      emit: (_sender, packet) => output.push(packet), now: () => 0,
      dial: () => { queueMicrotask(() => socket.open()); return socket as unknown as WebSocket } })
    const opened = await relay.open(event, 'session-public')
    authorityNow = 360_001
    await relay.send(event, opened.nativeHostConnectionId, '{}')
    expect(socket.sends).toEqual(['{}']); expect(socket.ended).toBe(0)
    authorityNow = 115 * 60_000
    await expect(relay.send(event, opened.nativeHostConnectionId, '{}')).rejects.toThrow()
    expect(socket.ended).toBe(1); expect(socket.sends).toEqual(['{}'])
    expect(output.some(value => value.error === 'QWEN_NATIVE_SOCKET_OWNER_CHANGED')).toBe(true)
  })
  it('uses only validated workspace WSS/header auth and keeps early events with their real host ID', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    expect(ready).toEqual({ nativeHostConnectionId: 'host-1', model: config.model, voice: config.voice })
    const [url, options] = f.dial.mock.calls[0]
    expect(url).toBe('wss://public-workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime')
    expect(options).toMatchObject({ headers: { Authorization: `Bearer ${config.apiKey}` }, followRedirects: false,
      maxRedirects: 0, handshakeTimeout: 20000, maxPayload: 2097152, perMessageDeflate: false })
    expect(f.output).toEqual([{ hostConnectionId: 'host-1', data: JSON.stringify({ type: 'session.created', session: { id: 'public-provider-session' } }) }])
    expect(JSON.stringify(ready) + JSON.stringify(f.output)).not.toContain(config.apiKey)
    expect(money.deny).toHaveBeenCalledWith('native_realtime_socket_open')
    await f.relay.send(event, ready.nativeHostConnectionId, '{"type":"session.update"}')
    expect(f.sockets[0].sends).toEqual(['{"type":"session.update"}'])
    f.relay.close(event, ready.nativeHostConnectionId)
  })
  it.each(['consent', 'money', 'config', 'disabled'] as const)('rejects %s before any provider contact', async kind => {
    const f = fixture()
    if (kind === 'consent') f.state.configured = false
    if (kind === 'money') money.deny.mockImplementation(() => { throw Error('private money detail') })
    if (kind === 'config') f.load.mockResolvedValue({ ...config, endpoint: 'https://untrusted.example' })
    if (kind === 'disabled') f.load.mockResolvedValue({ ...config, enabled: false })
    await expect(f.relay.open(event, 'session-public')).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    expect(f.dial).not.toHaveBeenCalled()
    expect(f.ids.size).toBe(0)
  })
  it('checks consent freshly after actual open and closes the minted owner on rejection', async () => {
    const f = fixture(false), pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('QWEN_NATIVE_SOCKET_UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0)
    f.state.configured = false; f.sockets[0].open()
    await rejected
    expect(f.sockets[0].ended).toBe(1); expect(f.ids.size).toBe(0)
  })
  it('does not admit sends until the post-open fresh policy has completed', async () => {
    const f = fixture(); let call = 0
    f.policy.mockImplementation(async () => {
      if (++call === 3) return new Promise(() => undefined)
      return { capture: 'configured', runtimeEpoch: 3 }
    })
    const pending = f.relay.open(event, 'session-public'); void pending.catch(() => undefined)
    await vi.advanceTimersByTimeAsync(0)
    try { await expect(f.relay.send(event, 'host-1', '{}')).rejects.toThrow('UNCONFIRMED') }
    finally { f.relay.close(event, 'host-1') }
    expect(f.sockets[0].sends).toEqual([])
  })
  it('captures the owner before asynchronous configuration and never dials after it changes', async () => {
    const f = fixture(); let complete!: (value: typeof config) => void
    f.load.mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const pending = f.relay.open(event, 'session-public'); const rejected = expect(pending).rejects.toThrow('UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0)
    f.state.owner = false; complete(config); await rejected
    expect(f.dial).not.toHaveBeenCalled(); expect(f.ids.size).toBe(0)
  })
  it('never lets a wrong sender send, close, or retire the real owner', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    await expect(f.relay.send(wrong, ready.nativeHostConnectionId, '{}')).rejects.toThrow('UNCONFIRMED')
    expect(() => f.relay.close(wrong, ready.nativeHostConnectionId)).toThrow('UNCONFIRMED')
    expect(f.sockets[0].ended).toBe(0); expect(f.retire).not.toHaveBeenCalled()
    await f.relay.send(event, ready.nativeHostConnectionId, '{}')
    f.relay.close(event, ready.nativeHostConnectionId)
    expect(f.sockets[0].ended).toBe(1)
  })
  it.each(['send', 'receive', 'watch'] as const)('rechecks current process/epoch/connection on %s', async kind => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public'); f.output.length = 0
    f.state.owner = false
    if (kind === 'send') await expect(f.relay.send(event, ready.nativeHostConnectionId, '{}')).rejects.toThrow('UNCONFIRMED')
    if (kind === 'receive') f.sockets[0].message({ type: 'response.audio.delta', delta: 'public-audio' })
    if (kind === 'watch') await vi.advanceTimersByTimeAsync(200)
    expect(f.sockets[0].sends).toEqual([]); expect(f.sockets[0].ended).toBe(1)
    expect(f.output.every(x => !x.data)).toBe(true); expect(f.ids.size).toBe(0)
  })
  it('has one refresh in flight and closes when the last successful policy expires', async () => {
    const f = fixture(); await f.relay.open(event, 'session-public')
    const before = f.policy.mock.calls.length
    let finish!: (value: { capture: string; runtimeEpoch: number }) => void
    f.policy.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    await vi.advanceTimersByTimeAsync(2400)
    expect(f.policy.mock.calls.length).toBe(before + 1); expect(f.sockets[0].ended).toBe(0)
    await vi.advanceTimersByTimeAsync(200)
    expect(f.sockets[0].ended).toBe(1); expect(f.ids.size).toBe(0)
    finish({ capture: 'configured', runtimeEpoch: 3 }); await Promise.resolve()
    expect(f.sockets[0].ended).toBe(1)
  })
  it('refreshes consent and rejects an epoch/consent change', async () => {
    const f = fixture(); await f.relay.open(event, 'session-public')
    f.state.epoch++
    await vi.advanceTimersByTimeAsync(2000)
    expect(f.sockets[0].ended).toBe(1)
  })
  it.each([800, 1900])('keeps consent fresh when successful policy reads take %d ms', async delay => {
    const f = fixture(); await f.relay.open(event, 'session-public')
    f.policy.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve({capture: 'configured', runtimeEpoch: 3}), delay)))
    await vi.advanceTimersByTimeAsync(5000)
    expect(f.policy.mock.calls.length).toBeGreaterThanOrEqual(5)
    expect(f.sockets[0].ended).toBe(0)
    f.relay.retireOwner(7); await vi.advanceTimersByTimeAsync(delay + 200)
    expect(f.sockets[0].ended).toBe(1); expect(vi.getTimerCount()).toBe(0)
  })
  it.each(['client', 'backpressure', 'server', 'binary'] as const)('bounds %s and closes without retry or caching', async kind => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public'); f.output.length = 0
    if (kind === 'client') await expect(f.relay.send(event, ready.nativeHostConnectionId, '甲'.repeat(42667))).rejects.toThrow('UNCONFIRMED')
    if (kind === 'backpressure') { f.sockets[0].bufferedAmount = 2097152; await expect(f.relay.send(event, ready.nativeHostConnectionId, '{}')).rejects.toThrow('UNCONFIRMED') }
    if (kind === 'server') f.sockets[0].message('x'.repeat(2097153))
    if (kind === 'binary') f.sockets[0].message('{}', true)
    expect(f.sockets[0].ended).toBe(1); expect(f.sockets[0].sends).toEqual([])
    expect(f.output.every(x => !x.data)).toBe(true); expect(f.dial).toHaveBeenCalledTimes(1)
  })
  it.each([
    ['event', { socket_end_kind: 'provider_error_event', socket_provider_category: 'provider_unavailable' }],
    ['nested', { socket_end_kind: 'provider_response_failed', socket_provider_category: 'unknown' }],
    ['transport', { socket_end_kind: 'transport_error', socket_transport_category: 'reset' }],
  ] as const)('classifies the established %s failure without forwarding bodies or credentials', async (kind, end) => {
    const f = fixture(); await f.relay.open(event, 'session-public'); f.output.length = 0
    const secret = 'synthetic-4e722952300885b766e8f587' + config.apiKey
    if (kind === 'event') f.sockets[0].message({ type: 'error', error: { code: 'ServiceUnavailable', message: secret } })
    if (kind === 'nested') f.sockets[0].message({ type: 'response.done', response: { status: 'failed', status_details: { error: { code: 'PRIVATE_PROVIDER_CODE', message: secret } } } })
    if (kind === 'transport') f.sockets[0].emit('error', Object.assign(Error(secret), { code: 'ECONNRESET' }))
    expect(f.establishedEnds).toHaveBeenCalledExactlyOnceWith(expect.objectContaining(end))
    expect(JSON.stringify([f.output, f.establishedEnds.mock.calls])).not.toContain(secret)
    expect(JSON.stringify(f.establishedEnds.mock.calls)).not.toContain('PRIVATE_PROVIDER_CODE')
    expect(f.output).toEqual([{ hostConnectionId: 'host-1', closed: true, error: 'QWEN_NATIVE_SOCKET_PROVIDER_ERROR' }])
  })
  it('distinguishes remote closure from local cleanup and measures application traffic separately from pings', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    await vi.advanceTimersByTimeAsync(100)
    await f.relay.send(event, ready.nativeHostConnectionId, '{"type":"session.update"}')
    await vi.advanceTimersByTimeAsync(900)
    f.sockets[0].message({ type: 'session.updated' })
    await vi.advanceTimersByTimeAsync(30_000)
    f.sockets[0].pong()
    f.sockets[0].emit('close', 1012, Buffer.from('PRIVATE_CLOSE_REASON'))
    f.relay.close(event, ready.nativeHostConnectionId)
    expect(f.establishedEnds).toHaveBeenCalledExactlyOnceWith({ socket_end_kind: 'remote_close', socket_close_category: 'service_restart',
      elapsed_ms: 31_000, socket_application_idle_ms: 30_000 })
    expect(f.output.at(-1)).toEqual({ hostConnectionId: 'host-1', closed: true })
  })
  it('still retires and notifies the renderer if the established diagnostic callback throws', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    f.establishedEnds.mockImplementationOnce(() => { throw Error('diagnostic unavailable') })
    f.relay.close(event, ready.nativeHostConnectionId)
    expect(f.establishedEnds).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ socket_end_kind: 'local_close' }))
    expect(f.output.at(-1)).toEqual({ hostConnectionId: 'host-1', closed: true })
    expect(f.sockets[0].ended).toBe(1)
  })
  it('bounds handshake waiting, retaining no automatic reconnect', async () => {
    const f = fixture(false), pending = f.relay.open(event, 'session-public')
    const rejected = expect(pending).rejects.toThrow('UNCONFIRMED')
    await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(20001); await rejected
    expect(f.sockets[0].ended).toBe(1); expect(f.dial).toHaveBeenCalledTimes(1); expect(f.ids.size).toBe(0)
    expect(f.establishedEnds).not.toHaveBeenCalled()
  })
  it('internally retires navigation/destroyed senders even after public IPC admission stops', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    f.state.allowed = false
    expect(() => f.relay.close(event, ready.nativeHostConnectionId)).toThrow('UNCONFIRMED')
    f.relay.retireOwner(7)
    expect(f.sockets[0].ended).toBe(1); expect(f.ids.size).toBe(0)
  })
  it('rejects a late send completion after ownership changes; never replays its audio', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    f.sockets[0].holdSend = true
    const pending = f.relay.send(event, ready.nativeHostConnectionId, '{"type":"input_audio_buffer.append","audio":"public"}')
    const rejected = expect(pending).rejects.toThrow('UNCONFIRMED')
    f.state.owner = false; f.sockets[0].sent?.(); await rejected
    expect(f.sockets[0].sends).toHaveLength(1); expect(f.sockets[0].ended).toBe(1)
  })
  it('sends a bounded control-frame ping, accepts only its matching pong and keeps audio untouched', async () => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    await vi.advanceTimersByTimeAsync(30200)
    const socket = f.sockets[0]; expect(socket.pings).toHaveLength(1); expect(socket.sends).toEqual([])
    socket.pong(Buffer.from('not-the-current-ping'))
    await vi.advanceTimersByTimeAsync(9000); expect(socket.ended).toBe(0)
    socket.pong(); await vi.advanceTimersByTimeAsync(29999); expect(socket.ended).toBe(0); expect(socket.pings).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(201); expect(socket.pings).toHaveLength(2)
    f.relay.close(event, ready.nativeHostConnectionId); expect(vi.getTimerCount()).toBe(0)
  })
  it('retires a socket on missing pong and delivers the normal closed event without payload', async () => {
    const f = fixture(); await f.relay.open(event, 'session-public'); f.output.length = 0
    await vi.advanceTimersByTimeAsync(40200)
    expect(f.sockets[0].pings).toHaveLength(1); expect(f.sockets[0].ended).toBe(1)
    expect(f.output).toEqual([{hostConnectionId: 'host-1', closed: true, error: 'QWEN_NATIVE_SOCKET_UNCONFIRMED'}]); expect(vi.getTimerCount()).toBe(0)
    expect(f.establishedEnds).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ socket_end_kind: 'heartbeat_timeout' }))
    f.sockets[0].pong(); await vi.advanceTimersByTimeAsync(30000); expect(f.sockets[0].pings).toHaveLength(1)
  })
  it('sends a heartbeat even when every successful policy request is slow', async () => {
    const f = fixture(); await f.relay.open(event, 'session-public')
    f.policy.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve({capture: 'configured', runtimeEpoch: 3}), 800)))
    await vi.advanceTimersByTimeAsync(32000)
    expect(f.sockets[0].ended).toBe(0); expect(f.sockets[0].pings).toHaveLength(1)
    f.sockets[0].pong(); f.relay.retireOwner(7); await vi.advanceTimersByTimeAsync(1000)
    expect(vi.getTimerCount()).toBe(0)
  })
  it.each(['consent', 'owner'] as const)('cannot send after %s is revoked during a slow policy check', async reason => {
    const f = fixture(); await f.relay.open(event, 'session-public')
    f.policy.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve({capture: f.state.configured ? 'configured' : 'bounded_segmented', runtimeEpoch: 3}), 800)))
    await vi.advanceTimersByTimeAsync(29900)
    if (reason === 'consent') f.state.configured = false; else f.state.owner = false
    await vi.advanceTimersByTimeAsync(2000)
    expect(f.sockets[0].pings).toHaveLength(0); expect(f.sockets[0].ended).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
  })
  it('fails closed when the ping write fails without publishing provider detail', async () => {
    const f = fixture(); await f.relay.open(event, 'session-public'); f.output.length = 0
    await vi.advanceTimersByTimeAsync(30200); f.sockets[0].pingCallback?.(Error('private ping error'))
    expect(f.sockets[0].ended).toBe(1); expect(JSON.stringify(f.output)).not.toContain('private ping error')
    expect(f.output).toEqual([{hostConnectionId: 'host-1', closed: true, error: 'QWEN_NATIVE_SOCKET_UNCONFIRMED'}])
    expect(vi.getTimerCount()).toBe(0)
  })
  it.each(['close', 'retire', 'owner', 'consent'] as const)('does not send a heartbeat after %s', async reason => {
    const f = fixture(), ready = await f.relay.open(event, 'session-public')
    await vi.advanceTimersByTimeAsync(29900)
    if (reason === 'close') f.relay.close(event, ready.nativeHostConnectionId)
    if (reason === 'retire') f.relay.retireOwner(7)
    if (reason === 'owner') f.state.owner = false
    if (reason === 'consent') f.state.configured = false
    await vi.advanceTimersByTimeAsync(3000)
    expect(f.sockets[0].pings).toHaveLength(0); expect(f.sockets[0].ended).toBe(1); expect(vi.getTimerCount()).toBe(0)
  })
  it('ignores a late ping callback or pong after ownership has retired', async () => {
    const f = fixture(), first = await f.relay.open(event, 'session-public'); await vi.advanceTimersByTimeAsync(30200)
    const old = f.sockets[0], callback = old.pingCallback
    f.relay.close(event, first.nativeHostConnectionId); const second = await f.relay.open(event, 'session-public')
    old.pong(); callback?.(Error('private underlying detail'))
    expect(f.sockets[1].ended).toBe(0); expect(JSON.stringify(f.output)).not.toContain('private underlying detail')
    f.relay.close(event, second.nativeHostConnectionId); expect(vi.getTimerCount()).toBe(0)
  })

})
