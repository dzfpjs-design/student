/** Identity sidecar only. It never reads PCM/text, grants playback, or certifies native delivery. */
type PublicSource = Readonly<{ mainLeaseId: string; sessionId: string; childId: string; agentTurnId: string; speechKey: string }>
type Lease = Readonly<{ id: string; generation: number; sessionId?: string; kind: string }>
export type ManagedSourceMetadata = Readonly<{
  schema: 1; document_time_origin: number; source_ordinal: number; context_ordinal: number;
  main_lease_id: string; companion_lease_id: string; companion_generation: number;
  session_id: string; child_id: string; agent_turn_id: string; speech_key: string;
  pre_start_lease_kind: 'preparing' | 'managed'; source_start_confirmed: boolean;
  native_origin_verified: false; natural_end_verified: false;
}>
type Record = { context: object; buffer: object; metadata: ManagedSourceMetadata }
const providers = new WeakMap<object, PublicSource>()
const sources = new WeakMap<object, Record>()
const contexts = new WeakMap<object, number>()
let contextCount = 0, sourceCount = 0
const id = (x: unknown): x is string => typeof x === 'string' && /^[a-zA-Z0-9_-]{8,128}$/.test(x)
const sameLease = (m: ManagedSourceMetadata, lease: Lease | null | undefined): boolean => !!lease
  && lease.id === m.companion_lease_id && lease.generation === m.companion_generation
  && lease.sessionId === m.session_id && lease.kind === 'managed'

export function bindPublicReplyProviderMetadata(provider: object, source: PublicSource): void {
  try {
    if (!provider || providers.has(provider) || !source || ![source.mainLeaseId, source.sessionId, source.childId, source.agentTurnId].every(id)
      || source.speechKey !== `runtime-public-${source.agentTurnId}`) return
    providers.set(provider, Object.freeze({ ...source }))
  } catch { /* Sidecar failure cannot change the original public reply. */ }
}

export function bindManagedPlaybackSourceMetadata(source: AudioBufferSourceNode, context: AudioContext, buffer: AudioBuffer,
  provider: object, lease: Lease | null, options: Readonly<{ localPlaybackSessionId?: string; speechKey?: string; localOnly?: boolean }>): void {
  try {
    const publicSource = providers.get(provider)
    if (!publicSource || !lease || !id(lease.id) || !Number.isSafeInteger(lease.generation) || lease.generation < 1
      || !['preparing', 'managed'].includes(lease.kind) || options.localOnly === true
      || lease.sessionId !== publicSource.sessionId || options.localPlaybackSessionId !== publicSource.sessionId
      || options.speechKey !== publicSource.speechKey || source.context !== context || source.buffer !== buffer
      || sources.has(source) || !Number.isFinite(performance.timeOrigin) || performance.timeOrigin <= 0) return
    let ordinal = contexts.get(context)
    if (!ordinal) { ordinal = ++contextCount; contexts.set(context, ordinal) }
    const metadata: ManagedSourceMetadata = Object.freeze({ schema: 1, document_time_origin: performance.timeOrigin,
      source_ordinal: ++sourceCount, context_ordinal: ordinal, main_lease_id: publicSource.mainLeaseId,
      companion_lease_id: lease.id, companion_generation: lease.generation, session_id: publicSource.sessionId,
      child_id: publicSource.childId, agent_turn_id: publicSource.agentTurnId, speech_key: publicSource.speechKey,
      pre_start_lease_kind: lease.kind as 'preparing' | 'managed', source_start_confirmed: false,
      native_origin_verified: false, natural_end_verified: false })
    sources.set(source, { context, buffer, metadata })
  } catch { /* Metadata never changes allocation, native start, or callbacks. */ }
}

export function confirmManagedPlaybackSourceMetadata(source: AudioBufferSourceNode, lease: Lease | null, state: string): void {
  try {
    const record = sources.get(source)
    if (!record || record.metadata.source_start_confirmed || state !== 'speaking' || !sameLease(record.metadata, lease)
      || source.context !== record.context || source.buffer !== record.buffer) return
    record.metadata = Object.freeze({ ...record.metadata, source_start_confirmed: true })
  } catch { /* This is a start-result annotation, never a delivery acknowledgment. */ }
}

export function readManagedPlaybackSourceMetadata(source: AudioBufferSourceNode): ManagedSourceMetadata | null {
  try {
    const record = sources.get(source)
    if (!record || source.context !== record.context || source.buffer !== record.buffer
      || record.metadata.document_time_origin !== performance.timeOrigin) return null
    // Retain completed source identity for independent ended-event inspection.
    // Current ownership is checked by the observer/gate, never inferred here.
    return Object.freeze({ ...record.metadata })
  } catch { return null }
}

function exposeReadonlySurface(): void {
  try {
    if (typeof window === 'undefined') return
    if (Object.prototype.hasOwnProperty.call(window, '__hscManagedPlaybackSourceMetadata')) return
    Object.defineProperty(window, '__hscManagedPlaybackSourceMetadata', {
      value: Object.freeze({ readSource: readManagedPlaybackSourceMetadata }), writable: false, configurable: false,
    })
  } catch { /* Missing surface means diagnostic rejection; playback is unaffected. */ }
}
exposeReadonlySurface()
