import { denyUnpricedMoneyEgress } from './money-egress'
import WebSocket from 'ws'
import type { NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'
import type { NativeRealtimeInputSourceCommand, NativeRealtimeInputSourceScope, NativeRealtimeInputSource } from '../shared/nativeRealtimeInputSource'
import { validNativeRealtimeInputSource } from './native-realtime-input-source'
import { qwenOutputObject as object, type QwenOutputEnvelope } from '../shared/qwenOutputBridge'
import { QWEN_REALTIME_MODEL, validateQwenRealtimeConfig, type QwenRealtimeConfig } from './qwen-realtime'
import { QwenRuntimeOutputTurn } from './qwen-runtime-output-turn'

type Identity = Omit<NativeRealtimeOutputCommand, 'operation' | 'response_id' | 'issued_at'>
type Owner = {
  connectionId: string; voice: string; isCurrent: () => boolean
  authority: (command: NativeRealtimeOutputCommand) => Promise<Record<string, unknown>>
  readOriginalInput: (command: NativeRealtimeInputSourceCommand) => Promise<NativeRealtimeInputSource>
  resolveOriginalTool: (call: { callId: string; originalInput: NativeRealtimeInputSource }) => Promise<Identity | null>
  emit: (event: QwenOutputEnvelope) => void; onFailure: (code: string) => void
}

/** Owns one supplier socket, never an IPC authorization API. Input admission
 * and business delivery receipts must be supplied by the original Runtime. */
export class QwenRuntimeOutputConnection {
  readonly ready: Promise<void>
  private phase: 'created' | 'configuring' | 'ready' | 'admitting' | 'awaiting_tool' | 'resolving' | 'output' | 'ended' = 'created'
  private sessionUpdated: Record<string, unknown> | null = null
  private sessionId = ''
  private toolResponseId = ''
  private readonly consumedCalls = new Set<string>()
  private readonly consumedInputs = new Set<string>()
  private readonly receivedResponseIds = new Set<string>()
  private originalInput: NativeRealtimeInputSource | null = null
  private turn: QwenRuntimeOutputTurn | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private resolveReady!: () => void
  private rejectReady!: (error: Error) => void
  private readonly retired: Promise<never>
  private rejectRetired!: (error: Error) => void
  private closing: Promise<void> | null = null

  constructor(private readonly socket: WebSocket, private readonly owner: Owner) {
    this.ready = new Promise((resolve, reject) => { this.resolveReady = resolve; this.rejectReady = reject })
    // A cancelled owner may never await ready; cancellation is still observable.
    void this.ready.catch(() => undefined)
    this.retired = new Promise((_, reject) => { this.rejectRetired = reject })
    void this.retired.catch(() => undefined)
    this.timer = setTimeout(() => { void this.fail('HANDSHAKE_TIMEOUT') }, 10000)
    socket.on('message', this.message)
    socket.on('error', this.error)
    socket.on('close', this.closed)
  }

  close(): Promise<void> { return this.fail('CANCELLED') }

  /** Bind a durable original capture before requesting its supplier tool.
   * This only reads local facts: it cannot send input, grant cloud permission,
   * or replace an in-flight original with a newer sentence. */
  async expectOriginalInput(scope: NativeRealtimeInputSourceScope): Promise<void> {
    if (this.phase !== 'ready' || !this.current()) throw Error('QWEN_CONNECTION_INPUT_BUSY')
    const command = { ...scope, issued_at: new Date().toISOString() }
    if (command.connection_id !== this.owner.connectionId || this.consumedInputs.has(command.voice_input_id)
      || this.consumedInputs.size >= 256) throw Error('QWEN_CONNECTION_INPUT_REPLAY')
    this.phase = 'admitting'
    this.timer = setTimeout(() => { void this.fail('INPUT_TIMEOUT') }, 5000)
    try {
      const source = await Promise.race([this.owner.readOriginalInput(command), this.retired])
      if (this.phase !== 'admitting' || !this.current() || !validNativeRealtimeInputSource(source, command)
        || source.arrival_status !== 'not_dispatched') throw Error('changed')
      this.originalInput = Object.freeze({ ...source, scope: Object.freeze({ ...source.scope }) })
      this.consumedInputs.add(source.scope.voice_input_id)
      this.phase = 'awaiting_tool'; this.clearTimer()
      this.timer = setTimeout(() => { void this.fail('TOOL_TIMEOUT') }, 180000)
    } catch {
      await this.fail('INPUT_UNCONFIRMED')
      throw Error('QWEN_CONNECTION_INPUT_UNCONFIRMED')
    }
  }

  private message = (data: WebSocket.RawData, binary: boolean): void => {
    if (this.phase === 'ended') return
    if (!this.current()) { void this.fail('OWNER_RETIRED'); return }
    let event: Record<string, unknown>
    try {
      const bytes = Buffer.isBuffer(data) ? data : Array.isArray(data) ? Buffer.concat(data) : Buffer.from(data)
      if (binary || bytes.length > 750000) throw Error('invalid')
      event = object(JSON.parse(bytes.toString()))!
      if (!event || typeof event.type !== 'string') throw Error('invalid')
    } catch { void this.fail('EVENT_INVALID'); return }
    if (event.type === 'error') { void this.fail('SUPPLIER_FAILED'); return }
    if (event.type === 'response.created') {
      const response = object(event.response)
      if (!validId(response?.id) || this.receivedResponseIds.has(response.id) || this.receivedResponseIds.size >= 512) {
        void this.fail('RESPONSE_REPLAY'); return
      }
      this.receivedResponseIds.add(response.id)
    }
    if (this.phase === 'output') {
      const turn = this.turn!
      void turn.receive(event).then(() => {
        if (this.phase === 'output' && this.turn === turn && event.type === 'response.done') {
          this.turn = null; this.toolResponseId = ''; this.originalInput = null; this.phase = 'ready'
        }
      })
      return
    }
    if (event.type === 'session.created' && this.phase === 'created') {
      const session = object(event.session)
      if (!validId(session?.id)) { void this.fail('SESSION_UNCONFIRMED'); return }
      this.sessionId = session.id
      this.phase = 'configuring'
      try { this.send(sessionUpdate(this.owner.voice)) } catch { void this.fail('SEND_UNCONFIRMED') }
      return
    }
    if (event.type === 'session.updated' && this.phase === 'configuring') {
      const session = object(event.session), audio = object(session?.audio), output = object(audio?.output)
      const format = object(output?.format)
      // The official endpoint omits a disabled/null VAD field in its successful
      // ack. Only this socket's single pending explicit-null update can qualify
      // that omission; an unrequested or changed session cannot inherit it.
      if (!session || session.id !== this.sessionId || session.model !== QWEN_REALTIME_MODEL
        || (Object.hasOwn(session, 'turn_detection') && session.turn_detection !== null) || output?.voice !== this.owner.voice
        || format?.type !== 'pcm' || format?.sample_rate !== 24000) { void this.fail('SESSION_UNCONFIRMED'); return }
      this.sessionUpdated = { ...event, session: { ...session, turn_detection: null } }
      this.phase = 'ready'
      this.clearTimer(); this.resolveReady()
      return
    }
    if (event.type === 'session.updated' || event.type === 'session.created') { void this.fail('SESSION_CHANGED'); return }
    if (event.type === 'response.created') {
      const response = object(event.response)
      if (this.phase !== 'awaiting_tool' || !this.originalInput || this.toolResponseId || !validId(response?.id)) { void this.fail('RESPONSE_UNEXPECTED'); return }
      this.toolResponseId = response.id
      return
    }
    if (event.type === 'response.done') {
      const response = object(event.response), outputs = response?.output
      const call = Array.isArray(outputs) && outputs.length === 1 ? object(outputs[0]) : null
      if (this.phase !== 'awaiting_tool' || !this.originalInput || !this.sessionUpdated || response?.id !== this.toolResponseId
        || response?.status !== 'completed' || call?.type !== 'function_call' || call.status !== 'completed'
        || call.name !== 'handle_child_turn' || !validId(call.call_id) || typeof call.arguments !== 'string'
        || Buffer.byteLength(call.arguments) > 16384 || this.consumedCalls.has(call.call_id)
        || this.consumedCalls.size >= 256) { void this.fail('TOOL_UNCONFIRMED'); return }
      this.consumedCalls.add(call.call_id); this.phase = 'resolving'
      void this.resolveTool(event, call.call_id)
      return
    }
    if (String(event.type).startsWith('response.audio')) { void this.fail('UNAUTHORIZED_MEDIA') }
  }

  private async resolveTool(event: Record<string, unknown>, callId: string): Promise<void> {
    try {
      const original = this.originalInput!
      const command = { ...original.scope, issued_at: new Date().toISOString() }
      const fresh = await Promise.race([this.owner.readOriginalInput(command), this.retired])
      if (this.hasEnded()) return
      if (!this.current() || !validNativeRealtimeInputSource(fresh, command) || fresh.source_hash !== original.source_hash
        || fresh.transcript !== original.transcript || fresh.arrival_status !== 'not_dispatched') { await this.fail('INPUT_UNCONFIRMED'); return }
      // Model tool arguments may paraphrase or omit a sentence. Only the
      // persisted capture is handed to the original Runtime, never that rewrite.
      const identity = await Promise.race([this.owner.resolveOriginalTool({ callId, originalInput: original }), this.retired])
      if (this.hasEnded()) return
      if (!identity || identity.connection_id !== this.owner.connectionId || identity.call_id !== callId
        || identity.session_id !== original.scope.session_id || identity.child_id !== original.scope.child_id
        || identity.storage_generation !== original.scope.storage_generation
        || identity.voice !== this.owner.voice || !this.current()) { await this.fail('RUNTIME_UNCONFIRMED'); return }
      this.clearTimer()
      const turn = new QwenRuntimeOutputTurn({ identity, authority: async command => {
        const result = await this.owner.authority(command)
        if (command.operation === 'reserve') {
          const source = object(result.input_source)
          if (source?.voice_input_id !== original.scope.voice_input_id
            || source?.client_action_id !== 'realtime-' + original.scope.voice_input_id) throw Error('QWEN_CONNECTION_REPLY_SOURCE_CHANGED')
        }
        return result
      },
        isCurrent: () => this.phase === 'output' && this.current(), send: value => this.send(value),
        emit: this.owner.emit, onFailure: () => { void this.fail('OUTPUT_UNCONFIRMED') } })
      this.turn = turn; this.phase = 'output'
      if (!await turn.start(this.sessionUpdated, event)) await this.fail('OUTPUT_UNCONFIRMED')
    } catch { await this.fail('RUNTIME_UNCONFIRMED') }
  }

  private send(event: Record<string, unknown>): void {
    if (this.phase === 'ended' || !this.current() || this.socket.readyState !== WebSocket.OPEN
      || this.socket.bufferedAmount > 1000000) throw Error('QWEN_CONNECTION_UNAVAILABLE')
    this.socket.send(JSON.stringify(event), error => { if (error) void this.fail('SEND_UNCONFIRMED') })
  }
  private error = (): void => { void this.fail('TRANSPORT_FAILED') }
  private closed = (): void => { void this.fail('TRANSPORT_ENDED') }
  private current(): boolean { try { return this.owner.isCurrent() } catch { return false } }
  private hasEnded(): boolean { return this.phase === 'ended' }
  private clearTimer(): void { if (this.timer) clearTimeout(this.timer); this.timer = null }
  private fail(code: string): Promise<void> {
    if (this.phase === 'ended') return this.closing ?? Promise.resolve()
    this.phase = 'ended'; this.clearTimer(); this.originalInput = null
    this.rejectRetired(Error('QWEN_CONNECTION_RETIRED'))
    this.rejectReady(Error('QWEN_CONNECTION_' + code))
    this.socket.off('message', this.message)
    // Keep the error listener through ws teardown, including CONNECTING abort.
    try { this.socket.terminate() } catch { /* No reconnect or redispatch. */ }
    try { this.owner.onFailure('QWEN_CONNECTION_' + code) } catch { /* Already retired. */ }
    this.closing = (this.turn?.cancel() ?? Promise.resolve()).then(() => { this.turn = null })
    return this.closing
  }
}

const validId = (value: unknown): value is string => typeof value === 'string' && /^[a-zA-Z0-9_-]{8,128}$/.test(value)

function sessionUpdate(voice: string): Record<string, unknown> {
  return { type: 'session.update', session: {
    modalities: ['text', 'audio'], turn_detection: null,
    instructions: '每次用户发言只调用handle_child_turn。工具返回前保持安静，不解题，不自行执行。工具返回JSON后逐字播报response_text，不增删改写，不再调用工具。',
    input_audio_transcription: { model: 'qwen3-asr-flash-realtime' },
    audio: { input: { format: { type: 'pcm', sample_rate: 16000 } }, output: { voice, format: { type: 'pcm', sample_rate: 24000 } } },
    tools: [{ type: 'function', function: { name: 'handle_child_turn', description: '将完整原话交给原学习伙伴Runtime执行。',
      parameters: { type: 'object', additionalProperties: false, required: ['utterance'], properties: { utterance: { type: 'string' } } } } }],
  } }
}

/** Main private configuration only. No key, endpoint or arbitrary send API is
 * exposed to renderer; changing the selected credential also retires ownership. */
export function connectQwenRuntimeOutput(owner: Omit<Owner, 'voice'> & {
  currentConfig: () => QwenRealtimeConfig | null
}): QwenRuntimeOutputConnection {
  let config: QwenRealtimeConfig
  try {
    const selected = owner.currentConfig()
    if (!selected) throw Error('missing')
    config = validateQwenRealtimeConfig(selected)
    if (!config.enabled) throw Error('disabled')
  } catch { throw Error('QWEN_CONNECTION_CONFIG_UNAVAILABLE') }
  const url = new URL('/api-ws/v1/realtime', config.endpoint)
  url.protocol = 'wss:'; url.searchParams.set('model', config.model)
  denyUnpricedMoneyEgress('unwired_runtime_output')
  const socket = new WebSocket(url, { headers: { Authorization: `Bearer ${config.apiKey}` },
    followRedirects: false, handshakeTimeout: 10000, maxPayload: 750000, perMessageDeflate: false })
  return new QwenRuntimeOutputConnection(socket, { ...owner, voice: config.voice, isCurrent: () => {
    const selected = owner.currentConfig()
    return owner.isCurrent() && !!selected && selected.enabled && selected.apiKey.trim() === config.apiKey
      && selected.endpoint === config.endpoint && selected.model === config.model && selected.voice === config.voice
  } })
}
