import WebSocket, { type ClientOptions } from 'ws'
import { randomBytes } from 'node:crypto'
import type { IncomingMessage } from 'node:http'
import { denyUnpricedMoneyEgress } from './money-egress'
import { validateQwenRealtimeConfig, type QwenRealtimeConfig } from './qwen-realtime'
import type { QwenNativeSocketEvent, QwenNativeSocketOpen } from '../shared/qwenNativeSocket'

type Sender = { sender: { id: number } }
type Policy = { capture: string; runtimeEpoch: number }
type OpenStage = 'policy' | 'owner' | 'configuration' | 'preflight' | 'dial' | 'handshake' | 'post_open_policy'
type OpenFailure = 'timeout' | 'transport_error' | 'http_rejected' | 'socket_closed' | 'other'
type Retirement = 'none' | 'provider_error' | 'policy_unconfirmed' | 'owner_changed' | 'invalid_payload' | 'unconfirmed' | 'closed'
type PayloadFailure = 'none' | 'send_missing_socket' | 'send_not_opened' | 'send_not_ready' | 'send_invalid' | 'send_empty' | 'send_oversize' | 'message_conversion' | 'message_binary' | 'message_oversize' | 'message_json' | 'message_structure' | 'message_other'
type ProviderFailure = 'QWEN_NATIVE_CREDENTIAL_REJECTED' | 'QWEN_NATIVE_ACCESS_DENIED' | 'QWEN_NATIVE_ACCOUNT_BALANCE'
type ProviderCategory = 'credential' | 'access' | 'balance' | 'rate_limit' | 'model_not_found' | 'resource_not_found' | 'invalid_parameter' | 'quota' | 'provider_unavailable' | 'unknown'
type SocketEndKind = Exclude<Retirement, 'none' | 'closed'> | 'local_close' | 'local_failure' | 'remote_close'
  | 'transport_error' | 'heartbeat_timeout' | 'heartbeat_send_failed' | 'provider_error_event' | 'provider_response_failed'
type SocketCloseCategory = 'normal' | 'going_away' | 'no_status' | 'abnormal' | 'policy' | 'server_error' | 'service_restart' | 'try_later' | 'other'
type SocketTransportCategory = 'reset' | 'timeout' | 'unreachable' | 'broken_pipe' | 'protocol' | 'unknown'
type SocketEnd = {
  socket_end_kind: SocketEndKind
  socket_close_category?: SocketCloseCategory
  socket_transport_category?: SocketTransportCategory
  socket_provider_category?: ProviderCategory
}
type SocketEndDiagnostic = SocketEnd & { elapsed_ms: number; socket_application_idle_ms: number }
type FinishOptions = { announce?: boolean; payloadFailure?: PayloadFailure; end?: SocketEnd }

const closeCategories: Partial<Record<number, SocketCloseCategory>> = {
  1000: 'normal', 1001: 'going_away', 1005: 'no_status', 1006: 'abnormal', 1008: 'policy',
  1011: 'server_error', 1012: 'service_restart', 1013: 'try_later',
}
function transportCategory(cause: NodeJS.ErrnoException): SocketTransportCategory {
  switch (cause?.code) {
    case 'ECONNRESET': return 'reset'
    case 'ETIMEDOUT': return 'timeout'
    case 'ENETUNREACH': case 'EHOSTUNREACH': return 'unreachable'
    case 'EPIPE': return 'broken_pipe'
    case 'WS_ERR_INVALID_CLOSE_CODE': case 'WS_ERR_INVALID_OPCODE': return 'protocol'
    default: return 'unknown'
  }
}
// Only fixed, documented Model Studio error codes may cross the trust boundary.
// Neither the provider's message nor an unknown code becomes an IPC value.
function providerCategory(value: unknown): ProviderCategory {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'unknown'
  const record = value as Record<string, unknown>
  if (typeof record.code !== 'string' || typeof record.message !== 'string') return 'unknown'
  switch (record.code) {
    case 'InvalidApiKey': case 'invalid_api_key': return 'credential'
    case 'AccessDenied': case 'access_denied': case 'Endpoint.AccessDenied': case 'AccessDenied.Unpurchased': return 'access'
    case 'Arrearage': return 'balance'
    case 'Throttling.RateQuota': return 'rate_limit'
    case 'Throttling.AllocationQuota': case 'insufficient_quota': return 'quota'
    case 'ModelNotFound': return 'model_not_found'
    case 'NotFound': return 'resource_not_found'
    case 'InvalidParameter': return 'invalid_parameter'
    case 'InternalError': case 'ServiceUnavailable': return 'provider_unavailable'
    default: return 'unknown'
  }
}
const providerFailures: Partial<Record<ProviderCategory, ProviderFailure>> = {
  credential: 'QWEN_NATIVE_CREDENTIAL_REJECTED',
  access: 'QWEN_NATIVE_ACCESS_DENIED',
  balance: 'QWEN_NATIVE_ACCOUNT_BALANCE',
}
const providerFailure = (value: unknown): ProviderFailure | undefined => {
  return providerFailures[providerCategory(value)]
}
type FieldType = 'missing' | 'not_object' | 'null' | 'string' | 'number' | 'boolean' | 'array' | 'object'
type Shape = { socket_message_top: 'null' | 'array' | 'object' | 'scalar'; socket_message_type: 'missing' | 'non_string' | 'not_object'; socket_message_header: 'missing' | 'object' | 'other' | 'not_object'; socket_message_payload: 'missing' | 'object' | 'other' | 'not_object'; socket_message_error: 'missing' | 'object' | 'other' | 'not_object'; socket_message_code: 'missing' | 'object' | 'other' | 'not_object'; socket_message_message: 'missing' | 'object' | 'other' | 'not_object'; socket_message_event: 'missing' | 'object' | 'other' | 'not_object'; socket_message_code_type: FieldType; socket_message_message_type: FieldType; socket_provider_category: ProviderCategory }
type HttpRejection = { statusCode: number; socket_provider_category: ProviderCategory }
const shapeOf = (v: unknown): Shape => {
  const top = v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v === 'object' ? 'object' : 'scalar'
  const o = top === 'object' ? v as Record<string, unknown> : null
  const field = (k: string): 'missing' | 'object' | 'other' | 'not_object' => !o ? 'not_object' : !Object.hasOwn(o,k) ? 'missing' : o[k] !== null && typeof o[k] === 'object' && !Array.isArray(o[k]) ? 'object' : 'other'
  const kind = (k: string): FieldType => {
    if (!o) return 'not_object'
    if (!Object.hasOwn(o,k)) return 'missing'
    const value = o[k]
    if (value === null) return 'null'
    if (Array.isArray(value)) return 'array'
    if (typeof value === 'string') return 'string'
    if (typeof value === 'number') return 'number'
    if (typeof value === 'boolean') return 'boolean'
    return 'object'
  }
  // These are documented fixed Model Studio error codes, never raw provider
  // content. An untyped object with unknown fields remains an unknown failure.
  const category = o && !Object.hasOwn(o, 'type') ? providerCategory(o) : 'unknown'
  return { socket_message_top: top, socket_message_type: !o ? 'not_object' : !Object.hasOwn(o,'type') ? 'missing' : 'non_string', socket_message_header: field('header'), socket_message_payload: field('payload'), socket_message_error: field('error'), socket_message_code: field('code'), socket_message_message: field('message'), socket_message_event: field('event'), socket_message_code_type: kind('code'), socket_message_message_type: kind('message'), socket_provider_category: category }
}
function readHttpProviderCategory(response: IncomingMessage): Promise<ProviderCategory> {
  return new Promise(resolve => {
    const chunks: Buffer[] = []
    let size = 0
    const finish = (category: ProviderCategory): void => {
      clearTimeout(deadline)
      response.off('data', collect)
      response.off('end', parse)
      response.off('error', unavailable)
      response.off('close', unavailable)
      resolve(category)
    }
    const unavailable = (): void => finish('unknown')
    const collect = (chunk: Buffer): void => {
      if (!Buffer.isBuffer(chunk) || (size += chunk.length) > 4_096) { unavailable(); return }
      chunks.push(chunk)
    }
    const parse = (): void => {
      try {
        const body = JSON.parse(Buffer.concat(chunks, size).toString('utf8'))
        const category = providerCategory(body)
        finish(category === 'unknown' ? providerCategory(body?.error) : category)
      } catch { unavailable() }
    }
    const deadline = setTimeout(unavailable, 350)
    response.once('end', parse)
    response.once('error', unavailable)
    response.once('close', unavailable)
    response.on('data', collect)
  })
}
export type QwenNativeSocketOptions = {
  allowed: (event: Sender) => boolean
  current: (event: Sender, id: string, sessionId: string) => boolean
  register: (event: Sender) => string
  confirmModel?: (event: Sender, id: string, sessionId: string, model: string) => void
  retire: (event: Sender, id: string) => void
  policy: (event: Sender) => Promise<Policy>
  config: () => Promise<QwenRealtimeConfig | null>
  emit: (event: Sender, value: QwenNativeSocketEvent) => void
  onOpenFailure?: (stage: OpenStage, failure: OpenFailure, retirement: Retirement, payloadFailure: PayloadFailure, details: Shape | HttpRejection | null) => void
  onMessageShapeFailure?: (shape: Shape) => void
  onEstablishedEnd?: (details: SocketEndDiagnostic) => void
  dial?: (url: string, options: ClientOptions) => WebSocket
  now?: () => number
}
type Row = {
  event: Sender; id: string; sessionId: string; epoch: number; socket: WebSocket | null
  closed: boolean; opened: boolean; lastPolicy: number; lastPolicyAttempt: number
  policyFlight: Promise<void> | null; watch: ReturnType<typeof setInterval> | null
  retired: Promise<never>; rejectRetired: (error: Error) => void
  pending: Set<(error: Error) => void>
  retirement: Retirement; payloadFailure: PayloadFailure; shape: Shape | null; shapeReported: boolean; providerFailure: ProviderFailure | null
  nextPingAt: number; pingDeadline: number; pingPayload: Buffer | null
  openedAt: number | null; lastApplicationAt: number | null
}
const CLIENT_LIMIT = 128_000, SERVER_LIMIT = 2_097_152, POLICY_TTL = 2_500
const FAILURE = 'QWEN_NATIVE_SOCKET_UNCONFIRMED'
const PING_INTERVAL_MS = 30_000, PONG_TIMEOUT_MS = 10_000
const identifier = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value)
const error = (code = FAILURE): Error => Error(code)

/** Main owns the provider credential and each physical socket. No audio retry,
 * semantic fallback, or long-lived authority borrowed from a request callback. */
export class QwenNativeSocketRelay {
  private readonly rows = new Map<string, Row>()
  private readonly now: () => number
  constructor(private readonly options: QwenNativeSocketOptions) { this.now = options.now ?? Date.now }

  private allowed(event: Sender): boolean { try { return this.options.allowed(event) === true } catch { return false } }
  private current(row: Row): boolean {
    try { return !row.closed && this.allowed(row.event) && this.options.current(row.event, row.id, row.sessionId) === true } catch { return false }
  }
  private live(row: Row): boolean { return this.current(row) && this.now() - row.lastPolicy < POLICY_TTL }
  private emit(row: Row, value: QwenNativeSocketEvent): void {
    if (!this.allowed(row.event)) return
    try { this.options.emit(row.event, value) } catch { this.finish(row, 'QWEN_NATIVE_SOCKET_OWNER_CHANGED', { announce: false }) }
  }
  private finish(row: Row, code?: string, { announce = true, payloadFailure = 'none', end }: FinishOptions = {}): void {
    if (row.closed) return
    row.payloadFailure = code === 'QWEN_NATIVE_SOCKET_PAYLOAD_INVALID' ? payloadFailure : 'none'
    row.retirement = ({ QWEN_NATIVE_SOCKET_PROVIDER_ERROR: 'provider_error', QWEN_NATIVE_CREDENTIAL_REJECTED: 'provider_error',
      QWEN_NATIVE_ACCESS_DENIED: 'provider_error', QWEN_NATIVE_ACCOUNT_BALANCE: 'provider_error', QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED: 'policy_unconfirmed',
      QWEN_NATIVE_SOCKET_OWNER_CHANGED: 'owner_changed', QWEN_NATIVE_SOCKET_PAYLOAD_INVALID: 'invalid_payload',
      QWEN_NATIVE_SOCKET_UNCONFIRMED: 'unconfirmed' } as Record<string, Retirement>)[code ?? ''] ?? 'closed'
    row.closed = true
    if (row.watch !== null) clearInterval(row.watch)
    row.watch = null
    this.rows.delete(row.id)
    row.rejectRetired(error(code))
    for (const reject of [...row.pending]) reject(error(code))
    row.pending.clear()
    row.pingPayload = null
    try { row.socket?.terminate() } catch { /* Provider errors never enter IPC/logs. */ }
    try { this.options.retire(row.event, row.id) } catch { /* Internal close still owns the physical socket. */ }
    if (row.openedAt !== null && this.options.onEstablishedEnd) {
      try {
        const now = this.now()
        const fallbackKind = row.retirement === 'none' || row.retirement === 'closed'
          ? (code ? 'local_failure' : 'local_close') : row.retirement
        this.options.onEstablishedEnd({ ...end, socket_end_kind: end?.socket_end_kind ?? fallbackKind,
          elapsed_ms: Math.max(0, now - row.openedAt),
          socket_application_idle_ms: Math.max(0, now - Math.max(row.openedAt, row.lastApplicationAt ?? row.openedAt)),
        })
      } catch { /* Logging cannot interrupt socket cleanup or renderer notification. */ }
    }
    if (announce) this.emit(row, { hostConnectionId: row.id, closed: true, ...(code ? { error: code } : {}) })
  }
  private requireLive(row: Row): void {
    if (!this.live(row)) { this.finish(row, 'QWEN_NATIVE_SOCKET_OWNER_CHANGED'); throw error() }
  }
  private owned(event: Sender, id: string): Row {
    const row = this.rows.get(id)
    // A wrong sender cannot close, retire, or abort the original owner.
    if (!this.allowed(event) || !row || row.event.sender.id !== event.sender.id) throw error()
    this.requireLive(row)
    return row
  }
  private refresh(row: Row): Promise<void> {
    if (row.policyFlight) return row.policyFlight
    row.lastPolicyAttempt = this.now()
    const flight = Promise.resolve().then(() => this.options.policy(row.event)).then(policy => {
      if (!this.current(row) || policy.capture !== 'configured' || policy.runtimeEpoch !== row.epoch) throw error()
      row.lastPolicy = this.now()
    }).catch(() => { this.finish(row, 'QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED'); throw error() })
      .finally(() => {
        if (row.policyFlight === flight) { row.policyFlight = null; this.maybePing(row) }
      })
    row.policyFlight = flight
    return flight
  }
  private watch(row: Row): void {
    if (!this.live(row)) { this.finish(row, 'QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED'); return }
    try { denyUnpricedMoneyEgress('native_realtime_socket_active') } catch { this.finish(row, 'QWEN_NATIVE_SOCKET_UNCONFIRMED'); return }
    if (this.now() - row.lastPolicyAttempt >= 400 && !row.policyFlight) void this.refresh(row).catch(() => undefined)
    if (!row.opened || !row.socket) return
    if (row.pingPayload && this.now() >= row.pingDeadline) { this.finish(row, FAILURE, { end: { socket_end_kind: 'heartbeat_timeout' } }); return }
    this.maybePing(row)
  }
  /** Try again immediately after a successful slow policy check: a check can
   * otherwise occupy every watch tick once the heartbeat is due. */
  private maybePing(row: Row): void {
    if (row.closed || !row.opened || !row.socket || row.policyFlight || row.pingPayload || this.now() < row.nextPingAt) return
    try {
      this.requireLive(row)
      denyUnpricedMoneyEgress('native_realtime_socket_active')
      const payload = randomBytes(8)
      row.pingPayload = payload; row.pingDeadline = this.now() + PONG_TIMEOUT_MS
      row.socket.ping(payload, true, (error?: Error) => {
        if (error && !row.closed && row.pingPayload === payload) this.finish(row, FAILURE, { end: { socket_end_kind: 'heartbeat_send_failed' } })
      })
    } catch { this.finish(row, FAILURE) }
  }
  private async bounded<T>(work: Promise<T>, ms: number, row?: Row): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | null = null
    const expiry = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(error('QWEN_SOCKET_OPEN_TIMEOUT')), ms) })
    try { return await Promise.race(row ? [work, expiry, row.retired] : [work, expiry]) }
    finally { if (timer !== null) clearTimeout(timer) }
  }

  async open(event: Sender, sessionId: string): Promise<QwenNativeSocketOpen> {
    let row: Row | undefined
    let stage: OpenStage = 'policy', failure: OpenFailure = 'other'
    let httpRejection: HttpRejection | null = null
    try {
      if (!this.allowed(event) || !identifier(sessionId)) throw error()
      const policy = await this.bounded(this.options.policy(event), POLICY_TTL)
      if (!this.allowed(event) || policy.capture !== 'configured' || !Number.isSafeInteger(policy.runtimeEpoch) || policy.runtimeEpoch <= 0) throw error()
      // Capture the actual process/epoch/provider revision before loading config.
      stage = 'owner'
      const id = this.options.register(event)
      if (!identifier(id)) throw error()
      this.retireOwner(event.sender.id)
      let rejectRetired!: Row['rejectRetired']
      const retired = new Promise<never>((_, reject) => { rejectRetired = reject })
      void retired.catch(() => undefined)
      row = { event, id, sessionId, epoch: policy.runtimeEpoch, socket: null, closed: false, opened: false,
        lastPolicy: this.now(), lastPolicyAttempt: this.now(), policyFlight: null, watch: null, retired, rejectRetired, pending: new Set(), retirement: 'none', payloadFailure: 'none', shape: null, shapeReported: false, providerFailure: null,
        nextPingAt: 0, pingDeadline: 0, pingPayload: null, openedAt: null, lastApplicationAt: null }
      this.rows.set(id, row)
      const owned = row
      owned.watch = setInterval(() => this.watch(owned), 200)
      stage = 'configuration'
      const loaded = await this.bounded(this.options.config(), 5_000, owned)
      if (!loaded) throw error()
      const config = validateQwenRealtimeConfig(loaded)
      if (!config.enabled) throw error()
      stage = 'preflight'
      await this.bounded(this.refresh(owned), POLICY_TTL, owned)
      this.requireLive(owned)
      this.options.confirmModel?.(event, id, sessionId, config.model)
      denyUnpricedMoneyEgress('native_realtime_socket_open')
      const url = new URL('/api-ws/v1/realtime', config.endpoint)
      url.protocol = 'wss:'; url.searchParams.set('model', config.model)
      stage = 'dial'
      const socket = (this.options.dial ?? ((address, options) => new WebSocket(address, options)))(url.toString(), {
        headers: { Authorization: `Bearer ${config.apiKey}` }, followRedirects: false, maxRedirects: 0,
        handshakeTimeout: 20_000, maxPayload: SERVER_LIMIT, perMessageDeflate: false,
      })
      owned.socket = socket
      stage = 'handshake'
      const opened = new Promise<void>((resolve, reject) => {
        socket.on('open', () => { if (this.live(owned)) resolve(); else { this.finish(owned, 'QWEN_NATIVE_SOCKET_OWNER_CHANGED'); reject(error()) } })
        socket.on('message', (data: WebSocket.RawData, binary: boolean) => this.message(owned, data, binary))
        socket.on('pong', (data: Buffer) => {
          if (!this.live(owned)) { this.finish(owned, 'QWEN_NATIVE_SOCKET_OWNER_CHANGED'); return }
          if (owned.pingPayload && Buffer.isBuffer(data) && data.equals(owned.pingPayload)) {
            owned.pingPayload = null; owned.nextPingAt = this.now() + PING_INTERVAL_MS
          }
        })
        socket.on('error', cause => {
          if (failure === 'http_rejected') return
          failure = 'transport_error'
          this.finish(owned, 'QWEN_NATIVE_SOCKET_PROVIDER_ERROR', { end: {
            socket_end_kind: 'transport_error', socket_transport_category: transportCategory(cause),
          } })
          reject(error())
        })
        socket.on('unexpected-response', (_request, response) => {
          if (owned.closed || failure === 'http_rejected') return
          failure = 'http_rejected'
          const statusCode = response?.statusCode
          const fallback = statusCode === 401 ? 'credential' : statusCode === 403 ? 'access' : statusCode === 429 ? 'rate_limit' : 'unknown'
          if (typeof statusCode === 'number' && Number.isInteger(statusCode) && statusCode >= 100 && statusCode <= 599) {
            httpRejection = { statusCode, socket_provider_category: fallback }
          }
          const finishRejection = (category: ProviderCategory): void => {
            if (owned.closed) return
            const diagnosis = category === 'unknown' ? fallback : category
            if (httpRejection) httpRejection.socket_provider_category = diagnosis
            owned.providerFailure = providerFailures[diagnosis] ?? null
            this.finish(owned, owned.providerFailure ?? 'QWEN_NATIVE_SOCKET_PROVIDER_ERROR')
            reject(error())
          }
          if (statusCode && statusCode >= 400 && statusCode < 500) {
            void readHttpProviderCategory(response).then(finishRejection)
          } else finishRejection('unknown')
        })
        socket.on('close', code => {
          if (failure === 'http_rejected') return
          if (failure === 'other') failure = 'socket_closed'
          this.finish(owned, undefined, { end: { socket_end_kind: 'remote_close', socket_close_category: closeCategories[code] ?? 'other' } })
          reject(error())
        })
      })
      await this.bounded(opened, 20_000, owned)
      stage = 'post_open_policy'
      await this.bounded(this.refresh(owned), POLICY_TTL, owned)
      this.requireLive(owned)
      owned.openedAt = this.now()
      owned.nextPingAt = owned.openedAt + PING_INTERVAL_MS
      owned.opened = true
      return { nativeHostConnectionId: id, model: config.model, voice: config.voice }
    } catch (cause) {
      if (!httpRejection && cause instanceof Error && cause.message === 'QWEN_SOCKET_OPEN_TIMEOUT') failure = 'timeout'
      try { this.options.onOpenFailure?.(stage, failure, row?.retirement ?? 'none', row?.payloadFailure ?? 'none', httpRejection ?? row?.shape ?? null) } catch { /* Diagnostics do not control the socket. */ }
      if (row) this.finish(row, 'QWEN_NATIVE_SOCKET_UNCONFIRMED')
      throw error(row?.providerFailure ?? FAILURE)
    }
  }
  private message(row: Row, data: WebSocket.RawData, binary: boolean): void {
    if (row.closed) return
    if (!this.live(row)) { this.finish(row, 'QWEN_NATIVE_SOCKET_OWNER_CHANGED'); return }
    let payloadFailure: PayloadFailure = 'message_conversion'
    try {
      const bytes = Buffer.isBuffer(data) ? data : Array.isArray(data) ? Buffer.concat(data) : Buffer.from(data)
      payloadFailure = 'message_binary'
      if (binary) throw error()
      payloadFailure = 'message_oversize'
      if (bytes.length > SERVER_LIMIT) throw error()
      payloadFailure = 'message_json'
      const raw = bytes.toString('utf8'), value = JSON.parse(raw)
      payloadFailure = 'message_structure'
      if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.type !== 'string') {
        row.shape = shapeOf(value)
        // Once open has returned there will be no onOpenFailure callback.
        // First retirement closes the row; late/duplicate messages are ignored.
        if (row.opened && !row.shapeReported) {
          row.shapeReported = true
          try { this.options.onMessageShapeFailure?.(row.shape) } catch { /* Diagnostics never control the socket. */ }
        }
        // A top-level untyped response is classified only on an exact documented code.
        const known = value && typeof value === 'object' && !Array.isArray(value) && !Object.hasOwn(value, 'type') ? providerFailure(value) : undefined
        if (known) {
          row.providerFailure = known
          this.finish(row, known, { end: { socket_end_kind: 'provider_error_event', socket_provider_category: providerCategory(value) } })
          return
        }
        throw error()
      }
      payloadFailure = 'message_other'
      const failedResponse = value.response?.status === 'failed' || value.response?.status_details?.error
      if (value.type === 'error' || value.type.endsWith('.error') || value.error || failedResponse) {
        const known = providerFailure(value.error) ?? providerFailure(value.response?.status_details?.error)
        if (known) row.providerFailure = known
        const category = providerCategory(value.error)
        const responseCategory = providerCategory(value.response?.status_details?.error)
        this.finish(row, known ?? 'QWEN_NATIVE_SOCKET_PROVIDER_ERROR', { end: {
          socket_end_kind: failedResponse ? 'provider_response_failed' : 'provider_error_event',
          socket_provider_category: category !== 'unknown' ? category : responseCategory !== 'unknown' ? responseCategory : providerCategory(value),
        } })
        return
      }
      // A contradictory success event carrying an error envelope is never relayed.
      if (Object.hasOwn(value, 'code') && Object.hasOwn(value, 'message')) throw error()
      this.requireLive(row)
      row.lastApplicationAt = this.now()
      this.emit(row, { hostConnectionId: row.id, data: raw })
    } catch { this.finish(row, 'QWEN_NATIVE_SOCKET_PAYLOAD_INVALID', { payloadFailure }) }
  }
  async send(event: Sender, id: string, raw: string): Promise<void> {
    const row = this.owned(event, id), socket = row.socket
    const bytes = typeof raw === 'string' ? Buffer.byteLength(raw, 'utf8') : 0
    if (!socket || !row.opened || socket.readyState !== WebSocket.OPEN || !bytes || bytes > CLIENT_LIMIT) {
      const payloadFailure: PayloadFailure = !socket ? 'send_missing_socket' : !row.opened ? 'send_not_opened'
        : socket.readyState !== WebSocket.OPEN ? 'send_not_ready' : typeof raw !== 'string' ? 'send_invalid' : !bytes ? 'send_empty' : 'send_oversize'
      this.finish(row, 'QWEN_NATIVE_SOCKET_PAYLOAD_INVALID', { payloadFailure }); throw error()
    }
    if (!Number.isFinite(socket.bufferedAmount) || socket.bufferedAmount < 0 || socket.bufferedAmount + bytes > SERVER_LIMIT || row.pending.size >= 32) {
      this.finish(row, 'QWEN_NATIVE_SOCKET_BACKPRESSURE'); throw error()
    }
    try { denyUnpricedMoneyEgress('native_realtime_socket_send') } catch { this.finish(row, 'QWEN_NATIVE_SOCKET_UNCONFIRMED'); throw error() }
    await new Promise<void>((resolve, reject) => {
      let settled = false
      const finish = (failure?: Error): void => {
        if (settled) return
        settled = true; clearTimeout(timer); row.pending.delete(cancel)
        if (failure) reject(error()); else resolve()
      }
      const cancel = (failure: Error): void => finish(failure)
      const timer = setTimeout(() => { this.finish(row, 'QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED'); finish(error()) }, 5_000)
      row.pending.add(cancel)
      try {
        this.requireLive(row)
        socket.send(raw, { binary: false }, failure => {
          if (settled) return
          if (failure || !this.live(row)) { this.finish(row, 'QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED'); finish(error()); return }
          row.lastApplicationAt = this.now()
          finish()
        })
      } catch { this.finish(row, 'QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED'); finish(error()) }
    })
  }
  close(event: Sender, id: string): void {
    if (!this.allowed(event)) throw error()
    const row = this.rows.get(id)
    if (!row) return
    if (row.event.sender.id !== event.sender.id) throw error()
    this.finish(row)
  }
  /** Main navigation/process teardown must close even after IPC sender retires. */
  retireOwner(senderId: number): void {
    for (const row of [...this.rows.values()]) if (row.event.sender.id === senderId) this.finish(row)
  }
}
