/** Main-only money bridge. No IPC export and no request-supplied authority. */
import { createHash, createHmac, randomUUID, timingSafeEqual } from 'node:crypto'
import { lstatSync } from 'node:fs'
import { platform } from 'node:os'

export const MONEY_OWNER_SLOT = 'C:\\HSC\\test-cost-control\\joint8h-50cny-20261020'
const REQUEST_DOMAIN = 'joint8h_money_request_v1'
const RECEIPT_DOMAIN = 'joint8h_money_receipt_v1'
const fields = ['operation', 'request_id', 'attempt_id', 'step', 'session_id', 'child_id', 'agent_turn_id', 'connection_id', 'storage_generation', 'endpoint_origin', 'model', 'payload_sha256', 'claim_nonce', 'issued_at_ms'] as const
type Operation = 'pair.reserve' | 'pair.claim' | 'pair.closed' | 'pair.unknown'
type Command = Record<typeof fields[number], string | number>
type Receipt = { request_id: string; operation: Operation; attempt_id: string; step: number; state: string; claim_nonce: string; signature: string }
type Backend = { runtime: () => { status: string; localApiUrl: string; sessionToken: string }; seed: () => string | undefined }
let backend: Backend | null = null
let observedOwnerSlot = false

export function moneySlotPresent(): boolean {
  if (platform() !== 'win32') return false
  try { lstatSync(MONEY_OWNER_SLOT); observedOwnerSlot = true; return true }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      if (observedOwnerSlot) throw Error('MONEY_ACTIVE_LATCH_MISSING')
      return false
    }
    throw Error('MONEY_SLOT_UNCONFIRMED')
  }
}

export function configureMoneyBackend(value: Backend): void { backend = value }
export function assertMoneyLocalBackend(externalUrl?: string): void {
  if (moneySlotPresent() && externalUrl) throw Error('MONEY_EXTERNAL_BACKEND_FORBIDDEN')
}
export function denyUnpricedMoneyEgress(_operation: string): void {
  if (moneySlotPresent()) throw Error('MONEY_UNKNOWN_PRICING')
}
function key(seed: string | undefined): Buffer {
  if (!seed || !/^[a-f0-9]{64}$/.test(seed)) throw Error('MONEY_PRIVATE_AUTHORITY_UNAVAILABLE')
  return createHmac('sha256', Buffer.from(seed, 'hex')).update('joint8h_money_key_v1').digest()
}

export type MoneyPairIdentity = { session_id: string; child_id: string; agent_turn_id: string; connection_id: string; storage_generation: string }
export interface MoneyPair {
  beforeContact(): void
  assertCurrent(): void
  firstClosed(): Promise<void>
  beforeSecond(): Promise<void>
  completed(): Promise<void>
  uncertain(): Promise<void>
}

/** Reserve BOTH responses and claim0 before WebSocket constructor/session.update.
 * Each private object owns its one attempt. Lost RPC ACK is never retried.
 */
export async function beginMoneyPublicPair(identity: MoneyPairIdentity, config: { endpoint: string; model: string }, text: string, until: number): Promise<MoneyPair> {
  const active = moneySlotPresent()
  const context = backend
  const initial = context?.runtime()
  const seed = context?.seed()
  const attempt = 'pair-' + randomUUID().replaceAll('-', '')
  let phase: 'initial' | 'first' | 'between' | 'second' | 'ended' | 'unknown' = 'initial'
  let nonce = ''
  let contactUsed = false
  function current(): void {
    if (Date.now() >= until) throw Error('MONEY_EGRESS_DEADLINE')
    if (moneySlotPresent() !== active) throw Error('MONEY_ACTIVE_RESTART_REQUIRED')
    if (active) {
      assertMoneyLocalBackend(process.env.HSC_LOCAL_API_URL)
      const now = context?.runtime()
      if (!context || context !== backend || !initial || initial.status !== 'ready' || !now || now.status !== 'ready'
        || now.sessionToken !== initial.sessionToken || now.localApiUrl !== initial.localApiUrl || context.seed() !== seed) throw Error('MONEY_OWNER_CHANGED')
    }
  }
  async function request(operation: Operation, step: number): Promise<void> {
    current()
    if (!active) return
    const url = new URL(initial!.localApiUrl)
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw Error('MONEY_LOCAL_ONLY')
    const command: Command = { operation, request_id: randomUUID().replaceAll('-', ''), attempt_id: attempt, step,
      session_id: identity.session_id, child_id: identity.child_id, agent_turn_id: identity.agent_turn_id,
      connection_id: identity.connection_id, storage_generation: identity.storage_generation,
      endpoint_origin: config.endpoint, model: config.model,
      payload_sha256: createHash('sha256').update(text, 'utf8').digest('hex'),
      claim_nonce: operation === 'pair.closed' || operation === 'pair.unknown' ? nonce : '', issued_at_ms: Date.now() }
    const signingKey = key(seed)
    const signature = createHmac('sha256', signingKey).update(JSON.stringify([REQUEST_DOMAIN, ...fields.map(f => command[f])])).digest('hex')
    // Exactly one physical RPC. No catch/retry that can return an old nonce.
    const remaining = until - Date.now(); if (remaining <= 0) throw Error('MONEY_EGRESS_DEADLINE')
    const response = await fetch(`${url.origin}/v1/private/money-egress`, { method: 'POST', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-HSC-Session': initial!.sessionToken },
      signal: AbortSignal.timeout(Math.min(5000, remaining)), body: JSON.stringify({ ...command, signature }) })
    if (!response.ok || !response.body) throw Error('MONEY_RECEIPT_UNCONFIRMED')
    const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0
    try { for (;;) { const row = await reader.read(); if (row.done) break; size += row.value.byteLength; if (size > 4096) throw Error('MONEY_RECEIPT_UNCONFIRMED'); chunks.push(row.value) } }
    finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
    const result = JSON.parse(Buffer.concat(chunks).toString('utf8')) as Receipt
    if (Object.keys(result).sort().join(',') !== 'attempt_id,claim_nonce,operation,request_id,signature,state,step'
      || result.request_id !== command.request_id || result.operation !== operation || result.attempt_id !== attempt || result.step !== step
      || !/^[a-f0-9]{64}$/.test(result.signature)) throw Error('MONEY_RECEIPT_UNCONFIRMED')
    const expected = createHmac('sha256', signingKey).update(JSON.stringify([RECEIPT_DOMAIN, result.request_id, result.operation, result.attempt_id, result.step, result.state, result.claim_nonce])).digest()
    if (!timingSafeEqual(Buffer.from(result.signature, 'hex'), expected)) throw Error('MONEY_RECEIPT_UNCONFIRMED')
    if (operation === 'pair.claim') {
      if (result.state !== 'claimed' || !/^[a-f0-9]{32}$/.test(result.claim_nonce)) throw Error('MONEY_RECEIPT_UNCONFIRMED')
      nonce = result.claim_nonce
    } else if (result.state !== ({ 'pair.reserve': 'reserved', 'pair.closed': 'closed', 'pair.unknown': 'unknown' } as const)[operation] || result.claim_nonce !== '') throw Error('MONEY_RECEIPT_UNCONFIRMED')
    current()
  }
  await request('pair.reserve', 0)
  await request('pair.claim', 0)
  phase = 'first'
  return {
    assertCurrent() { current(); if (phase !== 'first' && phase !== 'second') throw Error('MONEY_DISPATCH_ALREADY_USED') },
    beforeContact() { current(); if (phase !== 'first' || contactUsed) throw Error('MONEY_DISPATCH_ALREADY_USED'); contactUsed = true },
    async firstClosed() { if (phase !== 'first') throw Error('MONEY_DISPATCH_ALREADY_USED'); phase = 'unknown'; await request('pair.closed', 0); phase = 'between'; nonce = '' },
    async beforeSecond() { if (phase !== 'between') throw Error('MONEY_DISPATCH_ALREADY_USED'); phase = 'unknown'; await request('pair.claim', 1); phase = 'second' },
    async completed() { if (phase !== 'second') throw Error('MONEY_DISPATCH_ALREADY_USED'); phase = 'unknown'; await request('pair.closed', 1); phase = 'ended'; nonce = '' },
    async uncertain() { const old = phase; phase = 'unknown'; if ((old === 'first' || old === 'second') && nonce) await request('pair.unknown', old === 'first' ? 0 : 1).catch(() => undefined) },
  }
}
