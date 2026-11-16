import type { NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'
import { qwenOutputObject as object, type QwenOutputEnvelope } from '../shared/qwenOutputBridge'
import { QwenOutputRelay } from './qwen-output-relay'

type Identity = Omit<NativeRealtimeOutputCommand, 'operation' | 'response_id' | 'issued_at'>
type Authority = (command: NativeRealtimeOutputCommand) => Promise<Record<string, unknown>>

/** Main-owned output phase of one original supplier tool call. Not an IPC API,
 * microphone transport, or authority to create a second semantic conversation. */
export class QwenRuntimeOutputTurn {
  private readonly identity: Identity
  private phase: 'new' | 'reserving' | 'creating' | 'binding' | 'streaming' | 'ended' = 'new'
  private responseId = ''
  private relay: QwenOutputRelay | null = null
  private tail: Promise<void> = Promise.resolve()
  private pendingBytes = 0
  private pendingEvents = 0
  private retirePromise: Promise<void> | null = null
  private deadline: ReturnType<typeof setTimeout> | null = null
  private reserveRequest: Promise<Record<string, unknown>> | null = null

  constructor(private readonly owner: {
    identity: Identity; authority: Authority; isCurrent: () => boolean
    send: (event: Record<string, unknown>) => void
    emit: (event: QwenOutputEnvelope) => void
    onFailure: (code: string) => void
  }) { this.identity = Object.freeze({ ...owner.identity }) }

  async start(sessionUpdated: unknown, originalToolDone: unknown): Promise<boolean> {
    if (this.phase !== 'new') return false
    const sessionEvent = object(sessionUpdated), session = object(sessionEvent?.session)
    const audio = object(session?.audio), output = object(audio?.output), format = object(output?.format)
    const toolEvent = object(originalToolDone), response = object(toolEvent?.response)
    const outputs = response?.output
    const tool = Array.isArray(outputs) && outputs.length === 1 ? object(outputs[0]) : null
    if (sessionEvent?.type !== 'session.updated' || session?.turn_detection !== null
      || output?.voice !== this.identity.voice || format?.type !== 'pcm' || format?.sample_rate !== 24000
      || toolEvent?.type !== 'response.done' || response?.status !== 'completed'
      || tool?.type !== 'function_call' || tool?.name !== 'handle_child_turn'
      || tool?.call_id !== this.identity.call_id || tool?.status !== 'completed') {
      await this.stop('SOURCE_UNCONFIRMED', false)
      return false
    }
    this.phase = 'reserving'
    try {
      if (!this.owner.isCurrent()) throw Error('retired')
      this.reserveRequest = this.request('reserve')
      const reserved = await this.reserveRequest
      if (!this.current('reserving')) { await this.stop('OWNER_RETIRED'); return false }
      if (reserved.state !== 'reserved' || reserved.local_only !== false || reserved.cloud_transmission_allowed !== true
        || typeof reserved.response_text !== 'string' || !reserved.response_text.trim()) throw Error('denied')
      // The Runtime source is consumed once. Never resend after uncertain send
      // or post-commit HTTP loss; readback cannot grant another dispatch.
      this.owner.send({ type: 'conversation.item.create', item: { type: 'function_call_output',
        call_id: this.identity.call_id, output: JSON.stringify({ response_text: reserved.response_text }) } })
      if (!this.current('reserving')) { await this.stop('OWNER_RETIRED'); return false }
      this.phase = 'creating'
      this.deadline = setTimeout(() => { void this.stop('TIMEOUT') }, 90_000)
      this.owner.send({ type: 'response.create' })
      if (!this.current('creating')) { await this.stop('OWNER_RETIRED'); return false }
      return true
    } catch { await this.stop('DISPATCH_UNCONFIRMED'); return false }
  }

  receive(value: unknown): Promise<void> {
    if (this.phase === 'ended') return Promise.resolve()
    let event: Record<string, unknown>, bytes: number
    try {
      const encoded = JSON.stringify(value)
      bytes = Buffer.byteLength(encoded)
      event = object(JSON.parse(encoded))!
      if (!event || bytes > 750000) throw Error('invalid')
    } catch { return this.stop('EVENT_INVALID') }
    if (++this.pendingEvents > 4098 || (this.pendingBytes += bytes) > 8000000) return this.stop('QUEUE_LIMIT')
    const work = this.tail.then(async () => {
      if (this.phase === 'ended') return
      if (!this.owner.isCurrent()) { await this.stop('OWNER_RETIRED'); return }
      if (event.type === 'response.created') {
        const response = object(event.response)
        if (this.phase !== 'creating' || typeof response?.id !== 'string'
          || !/^[a-zA-Z0-9_-]{8,128}$/.test(response.id)) { await this.stop('RESPONSE_UNEXPECTED'); return }
        this.responseId = response.id
        this.phase = 'binding'
        const bound = await this.request('bind')
        if (!this.current('binding')) { await this.stop('OWNER_RETIRED'); return }
        if (bound.state !== 'bound' || bound.dispatch_allowed !== true || bound.response_id !== this.responseId) {
          await this.stop('BIND_UNCONFIRMED'); return
        }
        const identity = this.identity
        this.relay = new QwenOutputRelay({ scope: { sessionId: identity.session_id, childId: identity.child_id,
          agentTurnId: identity.agent_turn_id, callId: identity.call_id, responseId: this.responseId, voice: identity.voice },
          localOnly: false, cloudTransmissionAllowed: true, isCurrent: () => this.current('streaming'),
          emit: this.owner.emit, onFailure: () => { void this.stop('SOURCE_FAILED') } })
        this.phase = 'streaming'
        return
      }
      if (event.type === 'session.updated' || String(event.type).startsWith('response.function_call')) {
        await this.stop('SOURCE_CHANGED'); return
      }
      if (!['response.audio.delta', 'response.audio.done', 'response.done', 'error'].includes(String(event.type))) return
      if (this.phase !== 'streaming' || !this.relay) { await this.stop('UNAUTHORIZED_MEDIA'); return }
      const receipt = await this.request('inspect')
      if (!this.current('streaming')) { await this.stop('OWNER_RETIRED'); return }
      if (receipt.state !== 'bound' || receipt.dispatch_allowed !== false || receipt.response_id !== this.responseId) {
        await this.stop('AUTHORITY_REVOKED'); return
      }
      const accepted = this.relay.receive(event)
      if (accepted === 'accepted' && event.type === 'response.done') await this.stop(null, false)
    }).catch(async () => { await this.stop('RECEIPT_UNCONFIRMED') }).finally(() => {
      this.pendingBytes -= bytes; this.pendingEvents--
    })
    this.tail = work
    return work
  }

  cancel(): Promise<void> { return this.stop('CANCELLED') }
  transportEnded(): Promise<void> { return this.phase === 'ended' ? Promise.resolve() : this.stop('TRANSPORT_ENDED') }

  private current(phase: typeof this.phase): boolean { return this.phase === phase && this.owner.isCurrent() }
  private request(operation: NativeRealtimeOutputCommand['operation']): Promise<Record<string, unknown>> {
    return this.owner.authority({ ...this.identity, operation, response_id: this.responseId, issued_at: new Date().toISOString() })
  }
  private stop(code: string | null, cancel = true): Promise<void> {
    if (this.phase === 'ended') return this.retirePromise ?? Promise.resolve()
    const authorityAttempted = this.phase !== 'new'
    const attempted = ['creating', 'binding', 'streaming'].includes(this.phase)
    this.phase = 'ended'
    if (this.deadline) clearTimeout(this.deadline)
    this.deadline = null
    this.relay?.retire()
    if (cancel && attempted) {
      try { this.owner.send({ type: 'response.cancel', ...(this.responseId ? { response_id: this.responseId } : {}) }) } catch { /* No redispatch. */ }
    }
    if (code) { try { this.owner.onFailure('QWEN_RUNTIME_OUTPUT_' + code) } catch { /* Ownership stays retired. */ } }
    if (!authorityAttempted) return Promise.resolve()
    this.retirePromise = (this.reserveRequest ?? Promise.resolve()).catch(() => undefined)
      .then(() => this.request('retire')).then(() => undefined, () => {
      try { this.owner.onFailure('QWEN_RUNTIME_OUTPUT_RETIRE_UNCONFIRMED') } catch { /* No permission restored. */ }
    })
    return this.retirePromise
  }
}
