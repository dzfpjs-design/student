import { PlaybackControlAdmission } from './playbackControlAdmission'
import { bindManagedPlaybackSourceMetadata, confirmManagedPlaybackSourceMetadata } from './managedPlaybackSourceMetadata'
import { isPetPcmControlReference, type PetPcmControlReference } from './petPlaybackReference'
import { requestVoiceSynthesis } from './api'
import { hasPcmTailRendered } from './pcmTailTiming'
import type { SpeechDisposition, SpeechPriority } from './speechArbiter'

/**
 * Playback boundary for the companion.  Providers run behind the local agent;
 * credentials must never be placed in the renderer.  The PCM contract makes
 * first-audio latency measurable and permits immediate cancellation.
 */
export type CompanionVoiceState = 'idle' | 'preparing' | 'speaking' | 'interrupted' | 'error'
export type CompanionSpeechOptions = { localPlaybackSessionId?: string; localOnly?: boolean; sourceAgentTurnId?: string; onPlaybackEnd?: () => void;
  priority?: SpeechPriority; expiresAt?: number; speechKey?: string; isCurrent?: () => boolean;
  beforePlayback?: (signal: AbortSignal) => Promise<boolean>;
  /** One bounded admission for a genuinely started managed attempt changing
   * to local fallback. Initial admission is never repeated. */
  beforeLocalFallback?: (signal: AbortSignal, lease: Readonly<CompanionPlaybackLease>) => Promise<boolean>;
  pcmControlReference?: Readonly<PetPcmControlReference>;
  onPreparationStart?: () => void;
  onNotDelivered?: (reason: Exclude<SpeechDisposition, 'finished'>) => void;
  /** Learning replies must keep one selected natural cloud voice. They must
   * never silently change into an operating-system voice after a provider
   * failure. Device and offline safety notices may still opt into fallback. */
  managedVoiceRequired?: boolean }

export type PcmChunk = { pcm16: ArrayBuffer; sampleRate: number; channels?: number }
export type TtsRequest = { text: string; requestId: string; signal: AbortSignal } & CompanionSpeechOptions
export type TtsProvider = {
  readonly id: string
  readonly isProductionVoice: boolean
  readonly localOnly?: boolean
  readonly localFallbackOnly?: boolean
  stream(request: TtsRequest): AsyncIterable<PcmChunk>
}

/** System speech is intentionally distinct from a managed natural-voice
 * stream, so callers cannot present a browser-installed voice as the selected
 * companion voice. */
export type CompanionSpeechResult = 'managed' | 'system_female_offline' | 'system_offline' | 'text_only'
const CACHEABLE_FIXED_TTS_TEXTS = new Set(['我们继续下一步。', '我在，慢慢说。', '我没有听清，请再说一遍。', '好的，我已停下。'])
const MAX_SCHEDULED_AUDIO_AHEAD_SECONDS = 4
const MAX_CACHE_ITEM_BYTES = 512_000
const MAX_CACHE_TOTAL_BYTES = 2_000_000
const ALLOWED_PCM_SAMPLE_RATES = new Set([8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000])
const MAX_PCM_FRAME_BYTES = 512_000
const MAX_PCM_RESPONSE_BYTES = 8_000_000
const MAX_PCM_COMPLETION_BYTES = 2048
export const MAX_MANAGED_PLAYBACK_SECONDS = 300
const PCM_READ_TIMEOUT_MS = 30_000

function boundedPcmOperation<T>(operation: Promise<T>, signal: AbortSignal, timeoutMs = PCM_READ_TIMEOUT_MS): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const cleanup = (): void => { clearTimeout(timer); signal.removeEventListener('abort', abort) }
    const abort = (): void => { cleanup(); reject(new DOMException('Playback cancelled', 'AbortError')) }
    const timer = setTimeout(() => { cleanup(); reject(new Error('TTS_STREAM_TIMEOUT')) }, timeoutMs)
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    operation.then(value => { cleanup(); resolve(value) }, error => { cleanup(); reject(error) })
  })
}

export function isCacheableFixedTtsText(text: string): boolean { return CACHEABLE_FIXED_TTS_TEXTS.has(text) }

/** Ephemeral, bounded cache for non-sensitive text only; never stores microphone audio. */
export class PcmPlaybackCache {
  private readonly values = new Map<string, PcmChunk[]>()
  private totalBytes = 0
  constructor(private readonly maxEntries = 20) {}
  get(key: string): PcmChunk[] | null {
    const hit = this.values.get(key)
    if (!hit) return null
    this.values.delete(key); this.values.set(key, hit)
    return hit.map((item) => ({ ...item, pcm16: item.pcm16.slice(0) }))
  }
  put(key: string, chunks: PcmChunk[]): void {
    const bytes = chunks.reduce((sum, item) => sum + item.pcm16.byteLength, 0)
    if (!chunks.length || key.length > 160 || bytes > MAX_CACHE_ITEM_BYTES) return
    const old = this.values.get(key); if (old) this.totalBytes -= old.reduce((sum, item) => sum + item.pcm16.byteLength, 0)
    this.values.delete(key)
    this.values.set(key, chunks.map((item) => ({ ...item, pcm16: item.pcm16.slice(0) })))
    this.totalBytes += bytes
    while (this.values.size > this.maxEntries || this.totalBytes > MAX_CACHE_TOTAL_BYTES) {
      const oldest = this.values.keys().next().value as string
      const removed = this.values.get(oldest) ?? []
      this.totalBytes -= removed.reduce((sum, item) => sum + item.pcm16.byteLength, 0)
      this.values.delete(oldest)
    }
  }
  clear(): void { this.values.clear(); this.totalBytes = 0 }
}

type VoiceListener = (state: CompanionVoiceState) => void
const listeners = new Set<VoiceListener>()
let state: CompanionVoiceState = 'idle'
let controller: AbortController | null = null
let browserUtterance: SpeechSynthesisUtterance | null = null
let browserPlaybackTimer: number | null = null
type PlaybackReleaseResult = Readonly<{ sourcesStopped: boolean; sourcesDisconnected: boolean; contextClosed: boolean }>
type OwnedPlayback = { context: AudioContext; sources: Set<AudioBufferSourceNode>; naturalEnd?: boolean; release?: Promise<PlaybackReleaseResult> }
const originalPlaybackReleases = new WeakMap<OwnedPlayback,PlaybackReleaseResult>()
// Actual owned contexts only: unresolved/failed release never becomes zero by
// a new player, generation change, elapsed time, or a renderer assertion.
let unreleasedOwnedPlaybacks = 0
let unconfirmedSystemPlaybackGeneration = 0
/** Opaque capability minted only after this actual managed player starts output. */
export type ManagedBargePlayback = Readonly<{ readonly player: unique symbol }>
export type OriginalPlaybackClosure = Readonly<{ readonly closure: unique symbol }>
export type ExactPlaybackCloseAck = Readonly<{ status: 'closed'|'unconfirmed' }>
type BargePlayer = { playback: OwnedPlayback; provider: LocalAgentStreamingTtsProvider; leaseId: string; generation: number;
  sessionId: string; agentTurnId: string; text: string; stopped: boolean }
const bargePlayers = new WeakMap<ManagedBargePlayback, BargePlayer>()
const exactCloseAcks = new WeakMap<ExactPlaybackCloseAck, BargePlayer>()
let activeBargeCapability: ManagedBargePlayback | null = null
type ClosurePlayer = {playback:OwnedPlayback;sessionId:string|undefined;generation:number;managed:ManagedBargePlayback|null;localPcm:boolean}
const closurePlayers = new WeakMap<OriginalPlaybackClosure,ClosurePlayer>()
let latestPlaybackClosure:OriginalPlaybackClosure|null=null
export function currentManagedBargePlayback(sessionId: string, agentTurnId: string): ManagedBargePlayback | null {
  const cap = activeBargeCapability, p = cap && bargePlayers.get(cap)
  return p && !p.stopped && activePlayback === p.playback && p.sessionId === sessionId && p.agentTurnId === agentTurnId
    && companionPlaybackLeaseMatches(p.leaseId,p.generation) ? cap : null
}
export function managedBargePlaybackCurrent(cap: ManagedBargePlayback): boolean {
  const p = bargePlayers.get(cap)
  return !!p && currentManagedBargePlayback(p.sessionId,p.agentTurnId) === cap
}
export function managedBargeNaturalReleaseConfirmed(cap:ManagedBargePlayback):boolean {
  const p=bargePlayers.get(cap),closed=p&&originalPlaybackReleases.get(p.playback)
  return !!p&&!p.stopped&&p.playback.naturalEnd===true&&playbackGeneration===p.generation&&state==='idle'
    &&closed?.sourcesStopped===true&&closed.sourcesDisconnected===true&&closed.contextClosed===true
}
/** Read-only closure reconciliation, never a capture/response or stop grant.
 * The origin must itself have naturally ended and closed. A successor must be
 * the latest real private player. A managed successor must match the current
 * ordinary prompt's agent and pass its unchanged natural-close check. Original
 * local PCM proves closure only; its existing cue/response gates still apply.
 * A browser/system speech callback cannot provide this native witness. The
 * owned-context count also covers every intervening PCM attempt, including
 * ones that never minted a barge capability or whose close is unconfirmed.
 */
export function originalPlaybackRecoveryClosure(origin:ManagedBargePlayback,sessionId:string,agentTurnId:string|undefined):OriginalPlaybackClosure|null {
  const first=bargePlayers.get(origin),cap=latestPlaybackClosure,last=cap&&closurePlayers.get(cap)
  const closed=first&&originalPlaybackReleases.get(first.playback)
  const latestClosed=last&&originalPlaybackReleases.get(last.playback)
  if(!first||first.sessionId!==sessionId||first.stopped||first.playback.naturalEnd!==true
    ||closed?.sourcesStopped!==true||closed.sourcesDisconnected!==true||closed.contextClosed!==true
    ||!cap||!last||last.sessionId!==sessionId||last.generation<first.generation
    ||last.generation!==playbackGeneration||state!=='idle'||activePlayback!==null
    ||last.playback.naturalEnd!==true||latestClosed?.sourcesStopped!==true
    ||latestClosed.sourcesDisconnected!==true||latestClosed.contextClosed!==true
    ||unreleasedOwnedPlaybacks!==0||unconfirmedSystemPlaybackGeneration>first.generation)return null
  if(last.managed){
    const managed=bargePlayers.get(last.managed)
    if(!managed||!managedBargeNaturalReleaseConfirmed(last.managed)
      ||(last.managed!==origin&&(!agentTurnId||managed.agentTurnId!==agentTurnId)))return null
  }else if(!last.localPcm)return null
  return cap
}
export function managedBargePlaybackReference(cap: ManagedBargePlayback): string | null {
  const p = bargePlayers.get(cap)
  return p && !p.stopped && managedBargePlaybackCurrent(cap) ? p.text : null
}
export function originalExactPlaybackCloseAck(cap: ManagedBargePlayback, ack: ExactPlaybackCloseAck): boolean {
  const p = bargePlayers.get(cap)
  return !!p && p.stopped && exactCloseAcks.get(ack) === p && ack.status === 'closed' && playbackGeneration === p.generation
}
export async function stopExactManagedBargePlayback(cap: ManagedBargePlayback): Promise<ExactPlaybackCloseAck|null> {
  const p = bargePlayers.get(cap)
  if (!p || !managedBargePlaybackCurrent(cap)) return null
  p.stopped = true
  stopCompanionSpeech()
  const result = await releaseOwnedPlayback(p.playback)
  const ack: ExactPlaybackCloseAck = Object.freeze({status: result.sourcesStopped && result.sourcesDisconnected && result.contextClosed ? 'closed' : 'unconfirmed'})
  exactCloseAcks.set(ack,p)
  return ack
}
let activePlayback: OwnedPlayback | null = null
let externalPlaybackOwner: string | null = null
let playbackLocalOnly = true
let activeSpeechKey: string | undefined
export type CompanionPlaybackLease = { id: string; generation: number; sessionId?: string; kind: 'preparing' | 'managed' | 'local_sapi' | 'local_pcm' | 'unverified_sapi'; text: string | null; pcmReference?: Readonly<PetPcmControlReference> }
let playbackGeneration = 0
let playbackLease: CompanionPlaybackLease | null = null
let playbackLeaseCurrent: (() => boolean) | undefined
let playbackControlAdmission: PlaybackControlAdmission | null = null
export function getCompanionPlaybackLease(includePreparing = false): CompanionPlaybackLease | null {
  try { return (state === 'speaking' || includePreparing && state === 'preparing') && playbackLease && (!playbackLeaseCurrent || playbackLeaseCurrent()) ? { ...playbackLease } : null }
  catch { return null }
}
export function companionPlaybackLeaseMatches(id: string, generation: number, includePreparing = false): boolean {
  const lease = getCompanionPlaybackLease(includePreparing)
  return lease?.id === id && lease.generation === generation
}
export function getCompanionPlaybackControlAdmission(id: string, generation: number, sessionId: string, childId: string): PlaybackControlAdmission | null {
  const lease = getCompanionPlaybackLease()
  if (!lease || lease.id !== id || lease.generation !== generation || lease.sessionId !== sessionId) return null
  return playbackControlAdmission?.bind(id, generation, sessionId, childId) ? playbackControlAdmission : null
}
function clearPlaybackControlAdmission(): void { playbackControlAdmission?.dispose(); playbackControlAdmission = null }
function beginPlaybackLease(options: CompanionSpeechOptions, kind: CompanionPlaybackLease['kind'], text: string | null): void {
  clearPlaybackControlAdmission()
  playbackLease = { id: crypto.randomUUID(), generation: ++playbackGeneration, sessionId: options.localPlaybackSessionId, kind, text,
    ...(kind === 'local_pcm' && options.localOnly === true && isPetPcmControlReference(options.pcmControlReference)
      && options.pcmControlReference.session_id === options.localPlaybackSessionId
      ? { pcmReference: Object.freeze({ ...options.pcmControlReference }) } : {}) }
  playbackControlAdmission = new PlaybackControlAdmission(playbackLease.id, playbackLease.generation, playbackLease.sessionId)
  playbackLeaseCurrent = options.isCurrent
}
function updatePlaybackLease(kind: CompanionPlaybackLease['kind'], text: string | null): void {
  if (playbackLease) playbackLease = { ...playbackLease, kind, text }
}

function setState(next: CompanionVoiceState): void {
  state = next
  if (next === 'idle' || next === 'interrupted' || next === 'error') { activeBargeCapability = null; clearPlaybackControlAdmission(); playbackLease = null; playbackLeaseCurrent = undefined }
  listeners.forEach((listener) => {
    try { listener(next) }
    catch { console.warn('COMPANION_VOICE_STATE_CONSUMER_FAILED') }
  })
}

/** Release only this playback, once; one failed device operation cannot strand the rest. */
function releaseOwnedPlayback(playback: OwnedPlayback): Promise<PlaybackReleaseResult> {
  if (playback.release) return playback.release
  const sources = [...playback.sources]
  playback.sources.clear()
  if (activePlayback === playback) activePlayback = null
  playback.release = (async () => {
    let sourcesStopped = true, sourcesDisconnected = true, contextClosed = false
    for (const source of sources) {
      try { source.stop() }
      catch { sourcesStopped = false; console.warn('COMPANION_AUDIO_SOURCE_STOP_FAILED') }
      try { if (typeof source.disconnect !== 'function') sourcesDisconnected = false; source.disconnect?.() }
      catch { sourcesDisconnected = false; console.warn('COMPANION_AUDIO_SOURCE_DISCONNECT_FAILED') }
    }
    try { await boundedPcmOperation(playback.context.close(), new AbortController().signal, 1500); contextClosed = playback.context.state === 'closed' }
    catch { console.warn('COMPANION_AUDIO_CONTEXT_RELEASE_FAILED') }
    const result=Object.freeze({sourcesStopped,sourcesDisconnected,contextClosed})
    originalPlaybackReleases.set(playback,result)
    if(sourcesStopped&&sourcesDisconnected&&contextClosed)unreleasedOwnedPlaybacks--
    return result
  })()
  return playback.release
}

export function getCompanionVoiceState(): CompanionVoiceState { return state }
export function getCompanionPlaybackLocalOnly(): boolean { return playbackLocalOnly }
export function subscribeCompanionVoice(listener: VoiceListener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function hasCurrentSpeechLease(options: CompanionSpeechOptions): boolean {
  try { return !options.isCurrent || options.isCurrent() === true }
  catch { return false }
}

/** Bridges an audited external renderer (for example a WebRTC speech model)
 * into the same echo, interruption and blackboard timing boundary as local TTS. */
export function beginExternalCompanionSpeech(owner: string): void {
  if (!owner) return
  externalPlaybackOwner = owner
  clearPlaybackControlAdmission()
  playbackLease = null
  playbackLeaseCurrent = undefined
  activeSpeechKey = undefined
  playbackLocalOnly = true
  setState('speaking')
}

export function endExternalCompanionSpeech(owner: string, disposition: 'finished' | 'interrupted' | 'error' = 'finished'): void {
  if (!owner || externalPlaybackOwner !== owner) return
  externalPlaybackOwner = null
  setState(disposition === 'finished' ? 'idle' : disposition)
  if (disposition === 'interrupted') window.setTimeout(() => { if (state === 'interrupted') setState('idle') }, 180)
}

/** A local-agent endpoint, expected to yield framed signed 16-bit PCM chunks. */

export class LocalAgentStreamingTtsProvider implements TtsProvider {
  readonly id = 'local-agent-streaming-tts'
  readonly isProductionVoice = true
  // A backend policy denial must not be bypassed via a remote browser voice.
  readonly localFallbackOnly = true

  constructor(private readonly sessionId: string, private readonly options: CompanionSpeechOptions = {}) {}

  get localOnly(): boolean { return this.options.localOnly === true }

  async *stream(request: TtsRequest): AsyncIterable<PcmChunk> {
    if (this.options.localOnly || request.localOnly) throw new Error('LOCAL_ONLY_SYNTHESIS_REQUIRED')
    const transport = new AbortController()
    const abort = (): void => transport.abort()
    request.signal.addEventListener('abort', abort, { once: true })
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
    let pending = new Uint8Array()
    let wireBytes = 0, pcmBytes = 0, frameCount = 0, audioSeconds = 0
    let completed = false
    try {
      if (request.signal.aborted) throw new DOMException('Playback cancelled', 'AbortError')
      const response = await boundedPcmOperation(requestVoiceSynthesis(this.sessionId, request.text, request.requestId, transport.signal, {
        local_only: false,
        source_agent_turn_id: request.sourceAgentTurnId ?? this.options.sourceAgentTurnId,
        protocol: 'pcm16_frame_v2',
      }), transport.signal)
      if (!response.ok || !response.body) throw new Error('TTS_STREAM_UNAVAILABLE')
      reader = response.body.getReader()
      if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/x-hsc-pcm16-frame-v2') {
        throw new Error('TTS_STREAM_PROTOCOL_UNSUPPORTED')
      }
      while (true) {
        const next = await boundedPcmOperation(reader.read(), transport.signal)
        if (next.done) break
        if (completed && next.value.length) throw new Error('TTS_STREAM_AFTER_COMPLETION')
        wireBytes += next.value.length
        if (wireBytes > MAX_PCM_RESPONSE_BYTES + MAX_PCM_COMPLETION_BYTES + 8) throw new Error('TTS_STREAM_LIMIT_EXCEEDED')
        const joined = new Uint8Array(pending.length + next.value.length)
        joined.set(pending); joined.set(next.value, pending.length)
        let offset = 0
        // frame: uint32 little-endian sample rate, uint32 byte length, PCM bytes
        while (joined.length - offset >= 8) {
          if (completed) throw new Error('TTS_STREAM_AFTER_COMPLETION')
          const view = new DataView(joined.buffer, joined.byteOffset + offset)
          const sampleRate = view.getUint32(0, true)
          const length = view.getUint32(4, true)
          if (sampleRate === 0 ? length <= 0 || length > MAX_PCM_COMPLETION_BYTES
            : !ALLOWED_PCM_SAMPLE_RATES.has(sampleRate) || length <= 0 || length > MAX_PCM_FRAME_BYTES || length % 2 !== 0) {
            throw new Error('TTS_STREAM_PROTOCOL_INVALID')
          }
          if (joined.length - offset - 8 < length) break
          const pcm = joined.slice(offset + 8, offset + 8 + length)
          if (sampleRate === 0) {
            let manifest: Record<string, unknown>
            try { manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(pcm)) }
            catch { throw new Error('TTS_STREAM_COMPLETION_INVALID') }
            if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)
              || Object.keys(manifest).sort().join(',') !== 'frame_count,pcm_bytes,request_id,sample_count,type'
              || manifest.type !== 'generation_completed' || manifest.request_id !== request.requestId
              || !frameCount || manifest.frame_count !== frameCount || manifest.pcm_bytes !== pcmBytes
              || manifest.sample_count !== pcmBytes / 2) throw new Error('TTS_STREAM_COMPLETION_INVALID')
            completed = true
          } else {
            pcmBytes += length; frameCount++; audioSeconds += length / 2 / sampleRate
            if (pcmBytes + frameCount * 8 > MAX_PCM_RESPONSE_BYTES || frameCount > 600 || audioSeconds > MAX_MANAGED_PLAYBACK_SECONDS) {
              throw new Error('TTS_STREAM_LIMIT_EXCEEDED')
            }
            yield { pcm16: pcm.buffer, sampleRate }
          }
          offset += 8 + length
        }
        pending = joined.slice(offset)
        if (completed && pending.length) throw new Error('TTS_STREAM_AFTER_COMPLETION')
      }
      if (pending.length !== 0) throw new Error('TTS_STREAM_TRUNCATED')
      if (!completed) throw new Error('TTS_STREAM_COMPLETION_MISSING')
    } finally {
      request.signal.removeEventListener('abort', abort)
      transport.abort()
      if (reader) {
        void reader.cancel().catch(() => undefined)
        reader.releaseLock()
      }
    }
  }
}

async function playPcmStream(provider: TtsProvider, text: string, signal: AbortSignal, cache?: PcmPlaybackCache, onPlaybackStart?: () => void, options: CompanionSpeechOptions = {}, requireRenderedEnd = false, localPcmOwner = false): Promise<void> {
  if (!hasCurrentSpeechLease(options)) throw new DOMException('Playback owner retired', 'AbortError')
  const context = new AudioContext()
  const playback:OwnedPlayback = { context, sources: new Set<AudioBufferSourceNode>() }
  unreleasedOwnedPlaybacks++
  // Observation is minted by this original native owner, not renderer metadata.
  const closure=Object.freeze({})as OriginalPlaybackClosure
  const closurePlayer:ClosurePlayer={playback,sessionId:options.localPlaybackSessionId,generation:playbackLease?.generation??-1,managed:null,localPcm:localPcmOwner}
  closurePlayers.set(closure,closurePlayer);latestPlaybackClosure=closure
  activePlayback = playback
  const providerController = new AbortController()
  const abortProvider = (): void => providerController.abort()
  signal.addEventListener('abort', abortProvider, { once: true })
  if (signal.aborted) abortProvider()
  let iterator: AsyncIterator<PcmChunk> | undefined
  let rejectRender!: (error: Error) => void
  const renderFailure = new Promise<never>((_resolve, reject) => { rejectRender = reject })
  void renderFailure.catch(() => undefined)
  const checkLease = (): void => {
    if (hasCurrentSpeechLease(options)) return
    if (activePlayback === playback && controller?.signal === signal) stopCompanionSpeech()
    throw new DOMException('Playback owner retired', 'AbortError')
  }
  let renderTruncated = false
  const checkRenderContext = (): void => {
    if (requireRenderedEnd && typeof context.state === 'string' && context.state !== 'running') {
      throw new Error('LOCAL_AUDIO_CONTEXT_NOT_RUNNING')
    }
    if (renderTruncated) throw new Error('LOCAL_AUDIO_RENDER_TRUNCATED')
  }
  // This timer only revokes an obsolete owner; it never certifies audio delivery.
  const leaseTimer = options.isCurrent ? window.setInterval(() => {
    if (!hasCurrentSpeechLease(options) && activePlayback === playback && controller?.signal === signal) stopCompanionSpeech()
  }, 50) : null
  let when = context.currentTime
  const cacheKey = `${provider.id}:${text}`
  const cacheable = isCacheableFixedTtsText(text)
  const received: PcmChunk[] | null = cacheable ? [] : null
  let playbackStarted = false
  let scheduledBlocks = 0
  const rendered: Promise<void>[] = []
  try {
    if (requireRenderedEnd && context.state === 'suspended') await resumeOwnedAudioContext(context, signal)
    checkLease()
    if (signal.aborted) return
    // Test doubles written before AudioContext.state was observed may omit the
    // property. Real browsers must confirm a running context before a frame is
    // reported as audible; scheduling into a suspended context is not delivery.
    checkRenderContext()
    const cached = cacheable ? cache?.get(cacheKey) : null
    const stream = cached
      ? (async function* (): AsyncIterable<PcmChunk> { yield* cached })()
      : provider.stream({ text, requestId: crypto.randomUUID(), signal: providerController.signal, ...options })
    iterator = stream[Symbol.asyncIterator]()
    while (true) {
      const next = await boundedPcmOperation(Promise.race([iterator.next(), renderFailure]), signal)
      if (next.done) break
      const chunk = next.value
      checkLease()
      if (signal.aborted) return
      checkRenderContext()
      if (!cached && received) received.push({ ...chunk, pcm16: chunk.pcm16.slice(0) })
      const pcm = new Int16Array(chunk.pcm16)
      if (!pcm.length) continue
      const buffer = context.createBuffer(chunk.channels ?? 1, pcm.length, chunk.sampleRate)
      const channel = buffer.getChannelData(0)
      for (let index = 0; index < pcm.length; index += 1) channel[index] = pcm[index] / 32768
      await Promise.race([waitForAudioHeadroom(context, when, providerController.signal), renderFailure])
      checkLease()
      if (signal.aborted) return
      checkRenderContext()
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(context.destination)
      playback.sources.add(source)
      source.addEventListener('ended', () => playback.sources.delete(source), { once: true })
      when = Math.max(when, context.currentTime + 0.02)
      const scheduledEnd = when + buffer.duration
      const sourceOrdinal = ++scheduledBlocks
      const renderingSampleRate = Math.max(chunk.sampleRate, context.sampleRate ?? chunk.sampleRate)
      if (requireRenderedEnd) rendered.push(new Promise(resolve => {
        const done = (): void => { signal.removeEventListener('abort', done); resolve() }
        source.addEventListener('ended', () => {
          // A stopped source also emits ended; retain each block's own tail check.
          if (!signal.aborted && !hasPcmTailRendered(
            context.currentTime, scheduledEnd, sourceOrdinal, renderingSampleRate,
          )) {
            renderTruncated = true
            rejectRender(new Error('LOCAL_AUDIO_RENDER_TRUNCATED'))
          }
          done()
        }, { once: true })
        signal.addEventListener('abort', done, { once: true })
        if (signal.aborted) done()
      }))
      bindManagedPlaybackSourceMetadata(source, context, buffer, provider, playbackLease, options)
      source.start(when)
      if (!playbackStarted) {
        playbackStarted = true
        // Only an accepted managed stream establishes a public playback lane.
        // Preparation, offline fallback and original local PCM stay protected.
        playbackLocalOnly = options.localOnly === true || provider.localOnly === true || !provider.isProductionVoice
        updatePlaybackLease(playbackLocalOnly ? 'local_pcm' : 'managed', null)
        setState('speaking')
        // This is private player authority, never the diagnostic metadata sidecar.
        if (Object.getPrototypeOf(provider) === LocalAgentStreamingTtsProvider.prototype
          && provider.stream === LocalAgentStreamingTtsProvider.prototype.stream && !playbackLocalOnly
          && typeof options.sourceAgentTurnId === 'string' && options.sourceAgentTurnId.length > 0
          && typeof options.localPlaybackSessionId === 'string' && options.localPlaybackSessionId.length > 0
          && playbackLease?.kind === 'managed' && playbackLease.sessionId === options.localPlaybackSessionId
          && text.length <= 16000 && text.trim()) {
          const cap = Object.freeze({}) as ManagedBargePlayback
          bargePlayers.set(cap,{playback,provider:provider as LocalAgentStreamingTtsProvider,leaseId:playbackLease.id,
            generation:playbackLease.generation,sessionId:options.localPlaybackSessionId,agentTurnId:options.sourceAgentTurnId,text,stopped:false})
          activeBargeCapability = cap
          closurePlayer.managed = cap
        }
        onPlaybackStart?.()
      }
      confirmManagedPlaybackSourceMetadata(source, playbackLease, state)
      when += buffer.duration
    }
    if (!playbackStarted) throw new Error('EMPTY_TTS_AUDIO')
    // Keep the speaking state until the scheduled tail completes.
    // A stalled renderer may fail, but elapsed time never proves delivery.
    if (requireRenderedEnd) await boundedPcmOperation(Promise.race([Promise.all(rendered), renderFailure]), signal,
      Math.max(1500, (when - context.currentTime) * 1000 + 1500))
    else await waitForPlaybackTail(context, when, signal)
    checkLease()
    if (!signal.aborted) checkRenderContext()
    if (!signal.aborted && !cached && received) cache?.put(cacheKey, received)
    if (!signal.aborted) playback.naturalEnd = true
  } finally {
    if (leaseTimer !== null) window.clearInterval(leaseTimer)
    signal.removeEventListener('abort', abortProvider)
    providerController.abort()
    await releaseOwnedPlayback(playback)
    try {
      if (iterator?.return) await boundedPcmOperation(Promise.resolve(iterator.return()), new AbortController().signal, 1500)
    } catch { console.warn('COMPANION_PCM_STREAM_RELEASE_FAILED') }
  }
}

function resumeOwnedAudioContext(context: AudioContext, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException('Playback cancelled', 'AbortError'))
  return new Promise((resolve, reject) => {
    const deadline = performance.now() + 1500
    let settled = false
    const finish = (error?: unknown): void => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      signal.removeEventListener('abort', aborted)
      if (!error && performance.now() > deadline) error = new Error('LOCAL_AUDIO_RESUME_TIMEOUT')
      if (error) reject(error)
      else resolve()
    }
    const aborted = (): void => finish(new DOMException('Playback cancelled', 'AbortError'))
    const timer = window.setTimeout(() => finish(new Error('LOCAL_AUDIO_RESUME_TIMEOUT')), 1500)
    signal.addEventListener('abort', aborted, { once: true })
    try { void context.resume().then(() => finish(), finish) }
    catch (error) { finish(error) }
  })
}

/** Verified in-memory local PCM never falls back to synthesized text or a provider. */
export async function playLocalPcm(
  pcm: ArrayBuffer, sampleRate: number, onPlaybackStart?: () => void,
  options: CompanionSpeechOptions = {},
): Promise<'finished' | 'cancelled' | 'unavailable'> {
  if (!ALLOWED_PCM_SAMPLE_RATES.has(sampleRate) || !pcm.byteLength || pcm.byteLength % 2 || pcm.byteLength > Math.min(24 * 1024 * 1024, sampleRate * 2 * 600)) return 'unavailable'
  if (!hasCurrentSpeechLease(options)) return 'cancelled'
  stopCompanionSpeech()
  const current = new AbortController()
  controller = current
  activeSpeechKey = options.speechKey
  beginPlaybackLease(options, 'local_pcm', null)
  const provider: TtsProvider = {
    id: 'verified-local-pcm', isProductionVoice: false, localOnly: true,
    async *stream() {
      for (let offset = 0; offset < pcm.byteLength && !current.signal.aborted; offset += sampleRate * 2) {
        yield { pcm16: pcm.slice(offset, offset + sampleRate * 2), sampleRate, channels: 1 }
      }
    },
  }
  playbackLocalOnly = true
  setState('preparing')
  try {
    await playPcmStream(provider, 'local-audio', current.signal, undefined, onPlaybackStart, options, true, true)
    if (current.signal.aborted || !hasCurrentSpeechLease(options)) {
      if (controller === current) stopCompanionSpeech()
      return 'cancelled'
    }
    if (controller === current) setState('idle')
    if (current.signal.aborted || !hasCurrentSpeechLease(options)) return 'cancelled'
    options.onPlaybackEnd?.()
    return 'finished'
  } catch {
    if (current.signal.aborted) return 'cancelled'
    if (controller === current) setState('error')
    return 'unavailable'
  }
}

function waitForAudioHeadroom(context: AudioContext, scheduledUntil: number, signal: AbortSignal): Promise<void> {
  const delay = Math.max(0, scheduledUntil - context.currentTime - MAX_SCHEDULED_AUDIO_AHEAD_SECONDS) * 1000
  if (!delay || signal.aborted) return Promise.resolve()
  return new Promise((resolve) => {
    const timer = window.setTimeout(finish, delay)
    const abort = (): void => finish()
    function finish(): void { window.clearTimeout(timer); signal.removeEventListener('abort', abort); resolve() }
    signal.addEventListener('abort', abort, { once: true })
  })
}

function waitForPlaybackTail(context: AudioContext, when: number, signal: AbortSignal): Promise<void> {
  const milliseconds = Math.max(0, (when - context.currentTime) * 1000)
  return new Promise((resolve) => {
    const timer = window.setTimeout(finish, milliseconds)
    const abort = (): void => finish()
    function finish(): void {
      window.clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      resolve()
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}

function selectOfflineSystemVoice(localOnly: boolean): { voice: SpeechSynthesisVoice | null; isKnownFemale: boolean } {
  const voices = window.speechSynthesis.getVoices()
  const chinese = voices.filter((voice) => voice.lang.toLowerCase().startsWith('zh') && (!localOnly || voice.localService === true))
  // Windows 10/11 commonly exposes Xiaoxiao/Huihui/Yaoyao when an offline
  // Simplified-Chinese female system voice is installed. Never infer gender
  // from an arbitrary Chinese voice: return the generic label in that case.
  const female = chinese.find((voice) => /xiaoxiao|huihui|yaoyao|female|女/i.test(`${voice.name} ${voice.voiceURI}`))
  return { voice: female ?? chinese.find((voice) => voice.localService) ?? chinese[0] ?? null, isKnownFemale: Boolean(female?.localService) }
}

function waitForSystemVoice(localOnly: boolean, signal: AbortSignal): Promise<void> {
  const synthesis = window.speechSynthesis
  if (signal.aborted || selectOfflineSystemVoice(localOnly).voice || typeof synthesis.addEventListener !== 'function') return Promise.resolve()
  // Windows enumerates voices asynchronously on a fresh renderer. Keep the
  // first prompt pending, but never relax local-only routing or revive a stop.
  return new Promise((resolve) => {
    const timer = window.setTimeout(finish, 1500)
    function finish(): void {
      window.clearTimeout(timer)
      synthesis.removeEventListener('voiceschanged', changed)
      signal.removeEventListener('abort', finish)
      resolve()
    }
    function changed(): void { if (selectOfflineSystemVoice(localOnly).voice) finish() }
    synthesis.addEventListener('voiceschanged', changed)
    signal.addEventListener('abort', finish, { once: true })
    if (signal.aborted) finish()
    else changed()
  })
}

function playExplicitBrowserFallback(text: string, onPlaybackStart?: () => void, localOnly = false, onPlaybackEnd?: () => void, onUnavailable?: () => void): CompanionSpeechResult {
  if (!('speechSynthesis' in window)) return 'text_only'
  const selection = selectOfflineSystemVoice(localOnly)
  updatePlaybackLease(selection.voice?.localService === true ? 'local_sapi' : 'unverified_sapi', text)
  // An unset/default browser voice may be remote. A local-only request must
  // fail closed while local voices are unavailable or not yet enumerated.
  if (localOnly && !selection.voice) return 'text_only'
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.voice = selection.voice
  utterance.lang = 'zh-CN'; utterance.rate = 0.95
  const clearTimer = (): void => {
    if (browserPlaybackTimer !== null) window.clearTimeout(browserPlaybackTimer)
    browserPlaybackTimer = null
  }
  const unavailable = (): void => {
    if (browserUtterance !== utterance) return
    browserUtterance = null; clearTimer()
    window.speechSynthesis.cancel()
    setState('error'); onUnavailable?.()
  }
  utterance.onstart = () => {
    if (browserUtterance !== utterance) return
    // A real original system-speech start has no native close ACK here.
    // A later PCM completion cannot erase that unknown intervening output.
    unconfirmedSystemPlaybackGeneration = playbackGeneration
    clearTimer()
    setState('speaking')
    browserPlaybackTimer = window.setTimeout(unavailable, Math.min(120_000, Math.max(15_000, text.length * 600 + 8_000)))
    onPlaybackStart?.()
  }
  utterance.onend = () => {
    if (browserUtterance !== utterance) return
    browserUtterance = null
    clearTimer()
    setState('idle')
    onPlaybackEnd?.()
  }
  utterance.onerror = unavailable
  browserUtterance = utterance
  browserPlaybackTimer = window.setTimeout(unavailable, 5_000)
  window.speechSynthesis.speak(utterance)
  return selection.isKnownFemale ? 'system_female_offline' : 'system_offline'
}

/**
 * Uses a managed, natural voice only when its local-agent route is configured.
 * The browser voice is a visibly degradable accessibility fallback, never a
 * production-voice claim. Local-only requests require a verified local system
 * voice and never call a managed provider, even if local playback is unavailable.
 */
export async function speakCompanion(text: string, provider: TtsProvider | null = null, cache?: PcmPlaybackCache, onPlaybackStart?: () => void, options: CompanionSpeechOptions = {}): Promise<CompanionSpeechResult> {
  if (!hasCurrentSpeechLease(options)) return 'text_only'
  stopCompanionSpeech()
  if (!text.trim()) return 'text_only'
  const requestController = new AbortController()
  const localOnly = options.localOnly === true || provider?.localOnly === true
  controller = requestController
  activeSpeechKey = options.speechKey
  beginPlaybackLease(options, 'preparing', text)
  playbackLocalOnly = true
  setState('preparing')
  options.onPreparationStart?.()
  if (!localOnly && provider?.isProductionVoice) {
    try {
      await playPcmStream(provider, text, requestController.signal, cache, onPlaybackStart, options, true)
      if (requestController.signal.aborted || !hasCurrentSpeechLease(options)) {
        if (controller === requestController) stopCompanionSpeech()
        return 'text_only'
      }
      if (controller === requestController && !requestController.signal.aborted) setState('idle')
      if (requestController.signal.aborted || !hasCurrentSpeechLease(options)) return 'text_only'
      options.onPlaybackEnd?.()
      return 'managed'
    } catch {
      // Keep the same speech attempt alive while the local system voice takes
      // over.  Publishing an intermediate error would incorrectly cancel
      // synchronized projections such as the tutoring blackboard.
    }
  }
  if (requestController.signal.aborted || !hasCurrentSpeechLease(options)) {
    if (controller === requestController && !requestController.signal.aborted) stopCompanionSpeech()
    return 'text_only'
  }
  if (options.managedVoiceRequired) {
    setState('error')
    options.onNotDelivered?.('unavailable')
    return 'text_only'
  }
  const managedAttempt = getCompanionPlaybackLease()
  if (options.beforeLocalFallback && managedAttempt?.kind === 'managed') {
    // One five-second budget includes every durable transition. Preserve the
    // original controller and request expiry; an unknown ACK cannot resume.
    const remaining = Math.min(5000, options.expiresAt === undefined ? 5000 : options.expiresAt - Date.now())
    let timer: ReturnType<typeof setTimeout>|undefined
    let aborted = () => {}
    try {
      if (remaining <= 0) throw Error('LOCAL_FALLBACK_EXPIRED')
      const abort = new Promise<boolean>(resolve => {
        aborted = () => resolve(false)
        requestController.signal.addEventListener('abort', aborted, {once:true})
        timer = setTimeout(() => requestController.abort(), remaining)
      })
      const allowed = await Promise.race([options.beforeLocalFallback(requestController.signal, Object.freeze({...managedAttempt})), abort])
      if (!allowed || requestController.signal.aborted || !hasCurrentSpeechLease(options)) throw Error('LOCAL_FALLBACK_UNCONFIRMED')
    } catch {
      if (controller === requestController) stopCompanionSpeech()
      options.onNotDelivered?.('unavailable')
      return 'text_only'
    } finally {
      clearTimeout(timer)
      requestController.signal.removeEventListener('abort', aborted)
    }
  }
  if ('speechSynthesis' in window) {
    playbackLocalOnly = true
    setState('preparing')
    try {
      const localFallbackOnly = localOnly || provider?.localFallbackOnly === true
      if (!selectOfflineSystemVoice(localFallbackOnly).voice && typeof window.speechSynthesis.addEventListener === 'function') {
        await waitForSystemVoice(localFallbackOnly, requestController.signal)
      }
      if (requestController.signal.aborted) return 'text_only'
      const result = playExplicitBrowserFallback(text, onPlaybackStart, localFallbackOnly, () => {
        if (!requestController.signal.aborted) options.onPlaybackEnd?.()
      }, () => {
        if (!requestController.signal.aborted) options.onNotDelivered?.('unavailable')
      })
      if (result !== 'text_only') return result
    } catch {
      // A local engine failure must not send private text to another provider.
    }
  }
  setState('error')
  return 'text_only'
}

export function stopCompanionSpeech(speechKey?: string): void {
  if (speechKey !== undefined && speechKey !== activeSpeechKey) return
  const wasSpeaking = state === 'speaking' || state === 'preparing'
  controller?.abort(); controller = null
  const playback = activePlayback
  activePlayback = null
  if (playback) void releaseOwnedPlayback(playback)
  if ('speechSynthesis' in window) {
    try { window.speechSynthesis.cancel() }
    catch { console.warn('COMPANION_SYSTEM_SPEECH_CANCEL_FAILED') }
  }
  browserUtterance = null
  if (browserPlaybackTimer !== null) window.clearTimeout(browserPlaybackTimer)
  browserPlaybackTimer = null
  externalPlaybackOwner = null
  activeSpeechKey = undefined
  playbackLocalOnly = true
  setState(wasSpeaking ? 'interrupted' : 'idle')
  if (wasSpeaking) window.setTimeout(() => { if (state === 'interrupted') setState('idle') }, 180)
}
