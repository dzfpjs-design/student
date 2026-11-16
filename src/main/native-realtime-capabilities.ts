import { createHash, createHmac, randomUUID } from 'node:crypto'
import {
  nativeRealtimeCapabilityDomain, nativeRealtimeCapabilityProofFields,
  type NativeRealtimeCapabilityProof, type NativeRealtimeForward, type NativeRealtimeOperation,
} from '../shared/nativeRealtimeCapability'

type Sender = { sender: { id: number } }
type Owner = { status: string; localApiUrl: string; sessionToken: string; runtimeEpoch?: number; child: object | null; revision: number }
type Connection = { generation: number; sender: number; owner: Owner; session: string | null; expires: number; controller: AbortController }
type CompletionLease = { connection: Connection; host: string; dispatch: string; identity: Record<string, unknown>; expires: number; controller: AbortController }
type Options = {
  allowed: (event: Sender) => boolean
  current: () => Owner
  policy: (event: Sender) => Promise<{ capture: string; runtimeEpoch: number }>
  sign: (proof: NativeRealtimeCapabilityProof) => string
  fetch?: typeof fetch
  now?: () => number
}
const paths: Record<NativeRealtimeOperation, string> = {
  catalog: 'capabilities', context: 'native-context', input: 'native-input', proposal: 'capability-proposals',
  receipt: 'capability-receipt', dialogue: 'native-dialogue', client: 'native-client',
}
const callIdentity = ['version', 'voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id', 'call_id', 'input_source'] as const
const retiringCapabilities = new Set(['profile.switch', 'privacy.pause_all', 'mistake.open', 'reflection.complete'])
const identifier = (v: unknown): v is string => typeof v === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(v)
const same = (a: Owner, b: Owner): boolean => a.status === 'ready' && b.status === 'ready'
  && !!a.child && a.child === b.child && a.runtimeEpoch === b.runtimeEpoch && a.revision === b.revision
  && a.localApiUrl === b.localApiUrl && a.sessionToken === b.sessionToken
const fail = (): never => { throw Error('NATIVE_REALTIME_REQUEST_UNCONFIRMED') }

export function signNativeRealtimeCapability(key: string | undefined, proof: NativeRealtimeCapabilityProof): string {
  if (!key || !/^[a-f0-9]{64}$/.test(key)) return fail()
  return createHmac('sha256', Buffer.from(key, 'hex'))
    .update(JSON.stringify([nativeRealtimeCapabilityDomain, ...nativeRealtimeCapabilityProofFields.map(k => proof[k])]))
    .digest('hex')
}

/** A connection is minted only after actual Main-owned provider negotiation.
 * This is host request authority, never a claim that model prose is verified. */
export class NativeRealtimeCapabilityGateway {
  private generation = 0
  private readonly connections = new Map<string, Connection>()
  private readonly completionLeases = new Map<string, CompletionLease>()
  private readonly now: () => number
  constructor(private readonly options: Options) { this.now = options.now ?? Date.now }
  register(event: Sender): string {
    if (!this.options.allowed(event)) return fail()
    const owner = this.options.current()
    if (owner.status !== 'ready' || !owner.child || !owner.sessionToken
      || !Number.isSafeInteger(owner.runtimeEpoch) || owner.runtimeEpoch! <= 0) return fail()
    for (const [id, row] of this.connections) if (row.sender === event.sender.id) this.retire(id)
    const id = randomUUID()
    this.connections.set(id, { generation: ++this.generation, sender: event.sender.id, owner, session: null,
      expires: this.now() + 360_000, controller: new AbortController() })
    return id
  }
  /** Only Main's validated socket configuration may extend this connection.
   * Unknown models retain the original conservative six-minute bound. */
  confirmSocketModel(event: Sender, id: string, session: string, model: string): void {
    const row = this.owned(event, id, session)
    if (model === 'qwen3.8-omni-flash-realtime') row.expires = this.now() + 115 * 60_000
  }
  close(event: Sender, id: string): void {
    if (!this.options.allowed(event)) return fail()
    const row = this.connections.get(id)
    if (row && row.sender !== event.sender.id) return fail()
    this.retire(id)
  }
  isCurrent(event: Sender, id: string, session: string): boolean {
    try { this.owned(event, id, session); return true } catch { return false }
  }
  retireOwner(senderId: number): void {
    for (const [id, row] of this.connections) if (row.sender === senderId) this.retire(id)
    for (const [id, lease] of this.completionLeases) if (lease.connection.sender === senderId) {
      lease.controller.abort(); this.completionLeases.delete(id)
    }
  }
  private completionOwned(event: Sender, request: NativeRealtimeForward, body: Record<string, unknown>): CompletionLease {
    const token = request.completion_lease
    const lease = token && this.completionLeases.get(token)
    if (!lease || !this.options.allowed(event) || lease.connection.sender !== event.sender.id
      || request.operation !== 'client' || body.phase !== 'complete' || lease.host !== request.host_connection_id
      || lease.connection.session !== request.session_id || body.dispatch_id !== lease.dispatch
      || this.now() >= lease.expires || lease.controller.signal.aborted
      || !same(lease.connection.owner, this.options.current())
      || callIdentity.some(k => body[k] !== lease.identity[k])) return fail()
    return lease
  }
  private mintCompletion(event: Sender, id: string, row: Connection, body: Record<string, unknown>, result: Record<string, unknown>): Record<string, unknown> {
    if (body.phase !== 'claim' || result.status !== 'claimed') return result
    const dispatch = result.client_dispatch as Record<string, unknown> | undefined
    const turn = dispatch?.turn as Record<string, unknown> | undefined
    if (!dispatch || dispatch.version !== 1 || dispatch.status !== 'pending' || dispatch.dispatch_id !== body.dispatch_id
      || !identifier(dispatch.action_id) || !turn || !identifier(turn.agent_turn_id)) return fail()
    if (!retiringCapabilities.has(String(turn.runtime_capability))) return result
    for (const [key, lease] of this.completionLeases) if (this.now() >= lease.expires) {
      lease.controller.abort(); this.completionLeases.delete(key)
    }
    if (this.completionLeases.size >= 128) return fail()
    const token = randomUUID()
    this.completionLeases.set(token, { connection: row, host: id, dispatch: body.dispatch_id as string,
      identity: Object.fromEntries(callIdentity.map(k => [k, body[k]])), expires: this.now() + 180_000, controller: new AbortController() })
    // This never enters a tool's public_result or supplier context.
    return { ...result, completion_lease: token }
  }
  private retire(id: string): void { this.connections.get(id)?.controller.abort(); this.connections.delete(id) }
  private owned(event: Sender, id: string, session: string): Connection {
    const row = this.connections.get(id)
    if (!this.options.allowed(event) || !row || row.sender !== event.sender.id) return fail()
    if (row.controller.signal.aborted || this.now() >= row.expires
      || !same(row.owner, this.options.current())) { this.retire(id); return fail() }
    if (row.session !== null && row.session !== session) return fail()
    row.session = session
    return row
  }
  async forward(event: Sender, request: NativeRealtimeForward): Promise<unknown> {
    if (!request || !identifier(request.session_id) || !identifier(request.host_connection_id)
      || !Object.hasOwn(paths, request.operation)) return fail()
    const { session_id: session, host_connection_id: id, operation } = request
    const readOnly = operation === 'catalog' || operation === 'context'
    const body = readOnly ? undefined : { ...request.body }
    if (body) { delete body.host_connection_id; delete body.completion_lease; delete body.completion_after_retirement }
    const lease = request.completion_lease ? this.completionOwned(event, request, body ?? {}) : undefined
    const row = lease?.connection ?? this.owned(event, id, session)
    const checkOwned = (): void => { if (lease) this.completionOwned(event, request, body ?? {}); else this.owned(event, id, session) }
    if (lease && body) body.completion_after_retirement = true
    const voice = body?.voice_input_id
    if (!readOnly && operation !== 'input' && !identifier(voice)) return fail()
    if (operation === 'input' && voice !== undefined) return fail()
    let raw: string
    try { raw = body === undefined ? '' : JSON.stringify(body) } catch { return fail() }
    if (Buffer.byteLength(raw, 'utf8') > 128_000) return fail()
    const mayExport = async (): Promise<boolean> => {
      if (lease) return false
      try {
        const policy = await this.options.policy(event)
        this.owned(event, id, session)
        return policy.capture === 'configured' && policy.runtimeEpoch === row.owner.runtimeEpoch
      } catch { return false }
    }
    if (!lease && !['receipt', 'dialogue'].includes(operation) && !await mayExport()) return fail()
    checkOwned()
    let base: URL
    try { base = new URL(row.owner.localApiUrl) } catch { return fail() }
    if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
      || base.username || base.password || base.pathname !== '/' || base.search || base.hash) return fail()
    const proof: NativeRealtimeCapabilityProof = { version: 1, operation, session_id: session,
      voice_input_id: readOnly || operation === 'input' ? '' : voice as string, connection_id: id, connection_generation: row.generation,
      body_sha256: createHash('sha256').update(raw).digest('hex'), issued_at: new Date(this.now()).toISOString() }
    const signature = this.options.sign(proof)
    if (!/^[a-f0-9]{64}$/.test(signature)) return fail()
    const header = Buffer.from(JSON.stringify({ ...proof, signature }), 'utf8').toString('base64url')
    const signal = AbortSignal.any([(lease?.controller ?? row.controller).signal,
      AbortSignal.timeout(operation === 'proposal' || operation === 'client' && body?.phase === 'execute' ? 95_000 : 8_000)])
    const response = await (this.options.fetch ?? fetch)(`${base.origin}/v1/sessions/${encodeURIComponent(session)}/voice/realtime/${paths[operation]}`, {
      method: readOnly ? 'GET' : 'POST', redirect: 'error', cache: 'no-store', signal,
      headers: { 'X-HSC-Session': row.owner.sessionToken, 'X-HSC-Native-Voice-Proof': header,
        ...(readOnly ? {} : { 'Content-Type': 'application/json' }) }, ...(readOnly ? {} : { body: raw }),
    })
    if (!response.ok || !response.body) return fail()
    const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let bytes = 0
    try {
      for (;;) {
        const part = await reader.read(); if (part.done) break
        bytes += part.value.byteLength
        if (bytes > 262_144) return fail()
        chunks.push(part.value)
      }
    } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
    checkOwned()
    let result: unknown
    try { result = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { return fail() }
    if (!result || typeof result !== 'object' || Array.isArray(result)) return fail()
    if (lease) {
      const value = result as Record<string, unknown>
      const execution = value.execution as Record<string, unknown> | undefined
      if (value.version === 1 && ['completed', 'failed'].includes(String(execution?.status))) {
        this.completionLeases.delete(request.completion_lease!)
      }
      const { client_dispatch: _dispatch, ...receipt } = value
      return { ...receipt, public_result: { status: 'withheld', reason: 'retired_dispatch_completion_only' },
        public_projection: { eligible: false, verified_model_text: false } }
    }
    if (operation !== 'dialogue' && !await mayExport()) {
      if (operation !== 'receipt') return fail()
      return { ...result, public_result: { status: 'withheld', reason: 'consent_or_owner_changed' },
        public_projection: { eligible: false, verified_model_text: false } }
    }
    this.owned(event, id, session)
    return operation === 'client' ? this.mintCompletion(event, id, row, body!, result as Record<string, unknown>) : result
  }
}
