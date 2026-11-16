/** Metadata from existing App callbacks only. No capture, sampler, timer or admission. */
export type CaptureOrigin = 'app_native_function_candidate' | 'app_interposed_or_synthetic' | 'unknown'
export type CaptureBinding = { owner: string; sessionId: string; childId: string; generation: number;
  current: boolean; captureOwner: string | null; leaseId: string | null; deviceHash: string | null }
export type CaptureScope = { binding: () => CaptureBinding }
type Track = { id: string; kind: string; readyState: string; enabled: boolean; muted: boolean; getSettings: () => MediaTrackSettings; getConstraints?: () => MediaTrackConstraints }
type Stream = { getAudioTracks: () => Track[] }
type Sender = { track: Track | null }
type Peer = { connectionState: string; getSenders: () => Sender[] }
type Context = { state: string; sampleRate: number; currentTime: number }
type FrameEvent = { isTrusted: boolean; target: unknown; inputBuffer: { numberOfChannels: number; length: number; sampleRate: number } }
export type FrameMetadata = { current: boolean; trusted_callbacks: number; frames: number; bad_callbacks: number;
  media_time: number; sample_rate: number; last_frame_at: number; context_running: boolean }
export type CaptureState = { stream: Stream | null; root: Track | null; cloud: Track | null; peer: Peer | null; sender: Sender | null;
  captureCurrent: boolean; phase: 'listening' | 'protected' | 'blocked'; epoch: number; frame: FrameMetadata | null;
  documentTimeOrigin: number; now: number }
type AudioSource = { id?: string; trackIdentifier?: string; totalAudioEnergy?: number; totalSamplesDuration?: number }
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0
const bounded = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 160
const sha = async (v: string): Promise<string> => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(v)))].map(x => x.toString(16).padStart(2, '0')).join('')
const blocked = (reason: string) => ({ schema: 'current_app_mic_rtc_metadata_v1', status: 'unavailable', reason,
  native_origin_verified: false, synthetic_input_admitted: false, joint8h_admitted: false, acoustic_quality_verified: false })

/** Uses the already connected protected-control processor. Never reads channel samples. */
export class CaptureFrameMetadataCounter {
  private stream: Stream
  private context: Context
  private processor: unknown
  private callbacks = 0
  private frames = 0
  private bad = 0
  private mediaTime = 0
  private lastAt = 0
  private lastAdvanceAt: number | null = null
  constructor(stream: Stream, context: Context, processor: unknown) { this.stream = stream; this.context = context; this.processor = processor }
  observe(event: FrameEvent, now: number): void {
    try {
      const c = this.context, b = event.inputBuffer, mediaTime = c.currentTime
      if (event.isTrusted !== true || event.target !== this.processor || c.state !== 'running' || b.numberOfChannels !== 1
        || !integer(b.length) || b.length < 1 || b.length > 8192 || !finite(c.sampleRate) || c.sampleRate < 8000 || c.sampleRate > 96000
        || b.sampleRate !== c.sampleRate || !finite(mediaTime) || mediaTime < this.mediaTime || !finite(now) || now < this.lastAt
        || !Number.isSafeInteger(this.frames + b.length) || !Number.isSafeInteger(this.callbacks + 1)) { this.bad = Math.min(1_000_000, this.bad + 1); return }
      // Native callbacks queued on the main thread can share the rendering clock.
      // Only a strict clock advance refreshes media freshness; duplicates cannot keep it healthy.
      if (mediaTime > this.mediaTime) this.lastAdvanceAt = now
      this.frames += b.length; this.callbacks += 1; this.mediaTime = mediaTime; this.lastAt = now
    } catch { this.bad = Math.min(1_000_000, this.bad + 1) }
  }
  read(stream: Stream, root: Track, now: number): FrameMetadata {
    let exact = false
    try { const tracks = stream.getAudioTracks(); exact = stream === this.stream && tracks.length === 1 && tracks[0] === root } catch { /* unknown */ }
    return { current: exact && this.context.state === 'running' && this.bad === 0 && this.callbacks > 0 && finite(now) && now >= this.lastAt && now - this.lastAt <= 1000
      && this.lastAdvanceAt !== null && now >= this.lastAdvanceAt && now - this.lastAdvanceAt <= 1000,
      trusted_callbacks: this.callbacks, frames: this.frames, bad_callbacks: this.bad, media_time: this.mediaTime,
      sample_rate: this.context.sampleRate, last_frame_at: this.lastAt, context_running: this.context.state === 'running' }
  }
}

/** Native function syntax is a candidate label, never physical-device proof. */
export function classifyCaptureRoute(gum: unknown): CaptureOrigin {
  try { return typeof gum === 'function' && /\{\s*\[native code\]\s*\}/.test(Function.prototype.toString.call(gum)) ? 'app_native_function_candidate' : 'app_interposed_or_synthetic' }
  catch { return 'unknown' }
}

export class RealtimeCaptureMetadataObserver {
  private scope: CaptureScope
  private stream: Stream | null = null
  private root: Track | null = null
  private cloud: Track | null = null
  private origin: CaptureOrigin = 'unknown'
  private invalid = false
  private stats: { peer: Peer; cloud: Track; epoch: number; at: number; count: number; id: string; energy: number; duration: number } | null = null
  private statsReads = 0
  constructor(scope: CaptureScope) { this.scope = scope }
  opened(stream: Stream, root: Track, origin: CaptureOrigin): void {
    try { const tracks = stream.getAudioTracks(); if (this.root || tracks.length !== 1 || tracks[0] !== root || root.kind !== 'audio') { this.invalid = true; return }
      this.stream = stream; this.root = root; this.origin = origin
    } catch { this.invalid = true }
  }
  cloned(root: Track, cloud: Track): void {
    try {
      if (root !== this.root || this.cloud || cloud === root || cloud.kind !== 'audio' || cloud.id === root.id) { this.invalid = true; return }
      this.cloud = cloud
    } catch { this.invalid = true }
  }
  /** Called only by the existing bounded production getStats poll after its ownership check. */
  observeStats(peer: Peer, cloud: Track, epoch: number, sources: AudioSource[], now: number): void {
    this.stats = null
    try {
      if (this.invalid || cloud !== this.cloud || !integer(epoch) || !finite(now) || !Array.isArray(sources) || sources.length !== 1) return
      const row = sources[0]
      if (!bounded(row.id) || row.trackIdentifier !== cloud.id || !finite(row.totalAudioEnergy) || !finite(row.totalSamplesDuration) || row.totalSamplesDuration <= 0) return
      this.statsReads = Math.min(Number.MAX_SAFE_INTEGER, this.statsReads + 1)
      this.stats = { peer, cloud, epoch, at: now, count: this.statsReads, id: row.id, energy: row.totalAudioEnergy, duration: row.totalSamplesDuration }
    } catch { /* Observer failure never changes production polling or transport. */ }
  }
  invalidateStats(): void { this.stats = null }
  private checked(read: () => CaptureState): { state: CaptureState; binding: CaptureBinding; stats: RealtimeCaptureMetadataObserver['stats'] } | null {
    try {
      const s = read(), b = this.scope.binding(), stats = this.stats
      if (this.invalid || this.origin !== 'app_native_function_candidate' || !b.current || b.captureOwner !== b.owner
        || !bounded(b.owner) || !bounded(b.sessionId) || !bounded(b.childId) || !integer(b.generation) || b.generation < 1
        || !bounded(b.leaseId) || !/^[a-f0-9]{64}$/.test(b.deviceHash ?? '') || !s.captureCurrent
        || s.stream !== this.stream || s.root !== this.root || s.cloud !== this.cloud || !s.stream || !s.root || !s.cloud || !s.peer || !s.sender
        || !bounded(s.root.id) || !bounded(s.cloud.id) || s.root.id === s.cloud.id || s.root.readyState !== 'live' || s.cloud.readyState !== 'live' || !s.root.enabled || s.root.muted !== false
        || s.sender.track !== s.cloud || s.peer.connectionState !== 'connected'
        || !integer(s.epoch) || !finite(s.now)
        || !finite(s.documentTimeOrigin) || !s.frame?.current || s.frame.bad_callbacks !== 0
        || !['listening', 'protected'].includes(s.phase) || (s.phase === 'listening' ? !s.cloud.enabled : s.cloud.enabled)) return null
      const roots = s.stream.getAudioTracks(), senders = s.peer.getSenders().filter(x => x.track?.kind === 'audio')
      if (roots.length !== 1 || roots[0] !== s.root || senders.length !== 1 || senders[0] !== s.sender) return null
      const currentStats = stats && stats.peer === s.peer && stats.cloud === s.cloud && stats.epoch === s.epoch && s.now >= stats.at && s.now - stats.at <= 1000 ? stats : null
      if (s.phase === 'listening' && !currentStats) return null
      return { state: s, binding: b, stats: currentStats }
    } catch { return null }
  }
  async read(read: () => CaptureState): Promise<Record<string, unknown>> {
    const a = this.checked(read)
    if (!a) return blocked(this.origin === 'app_interposed_or_synthetic' ? 'SYNTHETIC_OR_INTERPOSED_SOURCE' : 'CURRENT_APP_CAPTURE_BINDING_UNPROVEN')
    const { state: s, binding: b, stats } = a
    let digests: string[]
    let initialDevice: string
    try {
      const device = s.root!.getSettings().deviceId
      if (typeof device !== 'string' || !device || device.length > 4096) return blocked('EXACT_NATIVE_DEVICE_HASH_UNPROVEN')
      initialDevice = device
      digests = await Promise.all([b.owner, b.sessionId, b.childId, b.leaseId!, s.root!.id, s.cloud!.id, stats?.id ?? '', device].map(sha))
      if (digests[7] !== b.deviceHash) return blocked('EXACT_NATIVE_DEVICE_HASH_UNPROVEN')
    }
    catch { return blocked('METADATA_DIGEST_UNAVAILABLE') }
    const z = this.checked(read)
    if (!z || z.state.peer !== s.peer || z.state.root !== s.root || z.state.cloud !== s.cloud || z.state.stream !== s.stream
      || z.state.sender !== s.sender || z.state.epoch !== s.epoch || z.state.phase !== s.phase || z.state.documentTimeOrigin !== s.documentTimeOrigin
      || JSON.stringify(z.binding) !== JSON.stringify(b) || z.stats?.id !== stats?.id || (stats && (!z.stats || z.stats.count < stats.count))) return blocked('METADATA_BRACKET_CHANGED')
    let settings: MediaTrackSettings, cloudSettings: MediaTrackSettings, constraints: MediaTrackConstraints
    try { settings = z.state.root!.getSettings(); cloudSettings = z.state.cloud!.getSettings(); constraints = z.state.root!.getConstraints?.() ?? {} } catch { return blocked('AEC_SETTINGS_UNAVAILABLE') }
    if (settings.deviceId !== initialDevice || cloudSettings.deviceId !== initialDevice) return blocked('EXACT_NATIVE_DEVICE_HASH_UNPROVEN')
    const mode = (echo: unknown) => echo === 'all' ? 'all' : echo === 'remote-only' ? 'remote-only' : echo === true ? 'browser-selected' : echo === false ? 'disabled' : 'unknown'
    const aec = mode(settings.echoCancellation), cloudAec = mode(cloudSettings.echoCancellation)
    const requested = constraints.echoCancellation as unknown
    const requestedExact = requested && typeof requested === 'object' && 'exact' in requested ? mode((requested as { exact: unknown }).exact) : 'unknown'
    const f = z.state.frame!
    return { schema: 'current_app_mic_rtc_metadata_v1', status: 'current_app_component_observed_unadmitted', origin: this.origin,
      owner_sha256: digests[0], session_sha256: digests[1], child_sha256: digests[2], lease_sha256: digests[3], root_track_sha256: digests[4],
      cloud_track_sha256: digests[5], rtc_source_sha256: z.stats ? digests[6] : null, device_sha256: b.deviceHash, connection_generation: b.generation,
      audio_health_epoch: z.state.epoch, document_time_origin: z.state.documentTimeOrigin, observed_at: z.state.now, phase: z.state.phase,
      root_ready_state: z.state.root!.readyState, cloud_ready_state: z.state.cloud!.readyState, root_enabled: z.state.root!.enabled,
      cloud_enabled: z.state.cloud!.enabled, root_muted: z.state.root!.muted, rtc_state: z.state.peer!.connectionState,
      current_audio_senders: 1, exact_root_clone_sender: true, lease_active_in_current_app: true, exact_registered_device_hash: true, frames: f.frames,
      trusted_callbacks: f.trusted_callbacks, bad_callbacks: f.bad_callbacks, media_time: f.media_time, sample_rate: f.sample_rate,
      last_frame_at: f.last_frame_at, context_running: f.context_running, rtc_stats_current: !!z.stats, rtc_stats_reads: z.stats?.count ?? 0, rtc_stats_observed_at: z.stats?.at ?? null,
      rtc_total_samples_duration: z.stats?.duration ?? null, rtc_total_audio_energy: z.stats?.energy ?? null, echo_cancellation: aec, cloud_echo_cancellation: cloudAec,
      requested_echo_cancellation: mode(requested), requested_exact_echo_cancellation: requestedExact,
      noise_suppression: typeof settings.noiseSuppression === 'boolean' ? settings.noiseSuppression : null,
      auto_gain_control: typeof settings.autoGainControl === 'boolean' ? settings.autoGainControl : null,
      observer_gum_calls: 0, observer_getstats_calls: 0, observer_contexts: 0, observer_timers: 0, raw_media_reads: 0, raw_media_writes: 0,
      native_origin_verified: false, synthetic_input_admitted: false, joint8h_admitted: false, acoustic_quality_verified: false }
  }
}

const readers = new Set<{ scope: CaptureScope; read: () => Promise<Record<string, unknown>> }>()
export function registerCurrentRealtimeMetadataReader(scope: CaptureScope, read: () => Promise<Record<string, unknown>>): () => void {
  if (readers.size >= 16) return () => undefined
  const entry = { scope, read }; readers.add(entry)
  return () => { readers.delete(entry) }
}
export async function readCurrentRealtimeCaptureMetadata(): Promise<Record<string, unknown>> {
  if (typeof window !== 'undefined' && window !== window.top) return blocked('CURRENT_TOP_DOCUMENT_REQUIRED')
  let current: { scope: CaptureScope; read: () => Promise<Record<string, unknown>> }[]
  try { current = [...readers].filter(x => { const b = x.scope.binding(); return b.current && b.captureOwner === b.owner }) }
  catch { return blocked('CAPTURE_OWNER_UNAVAILABLE') }
  if (current.length !== 1) return blocked('EXACT_ONE_CURRENT_APP_OBSERVER_REQUIRED')
  try { return await current[0].read() } catch { return blocked('CURRENT_APP_OBSERVER_FAILED') }
}
/** Public surface exposes a reader only, never stream/track/control/registration. */
if (typeof window !== 'undefined') {
  try { Object.defineProperty(window, '__hscReadCurrentRealtimeCaptureMetadata', { value: readCurrentRealtimeCaptureMetadata, writable: false, configurable: false }) }
  catch { /* A conflicting surface provides no trusted source admission. */ }
}

export function requireNativeJoint8hAdmission(..._untrusted: unknown[]): never {
  throw new Error('INDEPENDENT_ACTUAL_NATIVE_MIC_RTC_SOURCE_FRONT_DAILY_EXAM_PCM_ROOT_AND_CURRENT_JOINT8H_PROOF_REQUIRED')
}

/** Component-only live pair; no origin, clock, acoustic or journey acceptance. */
export function checkCurrentNativeMetadataPair(a: Record<string, unknown>, b: Record<string, unknown>, expected: Record<string, unknown>): Record<string, unknown> {
  const reject = (): never => { throw new Error('CURRENT_NATIVE_METADATA_PAIR_UNPROVEN') }
  const identities = ['owner_sha256', 'session_sha256', 'child_sha256', 'lease_sha256', 'device_sha256', 'root_track_sha256', 'cloud_track_sha256', 'rtc_source_sha256', 'connection_generation', 'audio_health_epoch', 'document_time_origin']
  if (identities.some(k => a[k] !== b[k]) || Object.keys(expected).length !== identities.length || identities.some(k => expected[k] !== a[k])) reject()
  for (const x of [a, b]) {
    if (x.schema !== 'current_app_mic_rtc_metadata_v1' || x.status !== 'current_app_component_observed_unadmitted' || x.origin !== 'app_native_function_candidate'
      || x.phase !== 'listening' || x.root_ready_state !== 'live' || x.cloud_ready_state !== 'live' || x.root_enabled !== true || x.cloud_enabled !== true
      || x.root_muted !== false || x.rtc_stats_current !== true || x.rtc_state !== 'connected' || x.current_audio_senders !== 1 || x.exact_root_clone_sender !== true || x.lease_active_in_current_app !== true || x.exact_registered_device_hash !== true
      || x.context_running !== true || x.bad_callbacks !== 0 || x.echo_cancellation !== x.cloud_echo_cancellation || !['all', 'remote-only', 'browser-selected'].includes(String(x.echo_cancellation))
      || ['observer_gum_calls', 'observer_getstats_calls', 'observer_contexts', 'observer_timers', 'raw_media_reads', 'raw_media_writes'].some(k => x[k] !== 0)
      || ['native_origin_verified', 'synthetic_input_admitted', 'joint8h_admitted', 'acoustic_quality_verified'].some(k => x[k] !== false)
      || identities.slice(0, 8).some(k => !/^[a-f0-9]{64}$/.test(String(x[k]))) || x.root_track_sha256 === x.cloud_track_sha256
      || !integer(x.connection_generation) || Number(x.connection_generation) < 1 || !integer(x.audio_health_epoch) || !finite(x.document_time_origin)
      || ['frames', 'trusted_callbacks', 'rtc_stats_reads'].some(k => !integer(x[k]) || Number(x[k]) < 1)
      || ['media_time', 'sample_rate', 'observed_at', 'last_frame_at', 'rtc_stats_observed_at', 'rtc_total_samples_duration', 'rtc_total_audio_energy'].some(k => !finite(x[k]))
      || Number(x.sample_rate) < 8000 || Number(x.sample_rate) > 96000 || Number(x.observed_at) < Number(x.last_frame_at) || Number(x.observed_at) - Number(x.last_frame_at) > 1000
      || Number(x.observed_at) < Number(x.rtc_stats_observed_at) || Number(x.observed_at) - Number(x.rtc_stats_observed_at) > 1000) reject()
  }
  if (Number(b.observed_at) <= Number(a.observed_at) || Number(b.observed_at) - Number(a.observed_at) > 5000
    || ['frames', 'trusted_callbacks', 'media_time', 'last_frame_at', 'rtc_stats_reads', 'rtc_stats_observed_at', 'rtc_total_samples_duration'].some(k => Number(b[k]) <= Number(a[k]))
    || Number(b.rtc_total_audio_energy) < Number(a.rtc_total_audio_energy) || a.sample_rate !== b.sample_rate) reject()
  return { status: 'source_component_pair_observed_unadmitted', native_origin_verified: false, joint8h_admitted: false, acoustic_quality_verified: false }
}
