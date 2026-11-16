import type { StandbyVoiceInput } from './types'
import type { ProtectedCaptureOwner } from './protectedPlaybackControl'
import { encodeControlWav } from './protectedPlaybackControl'
import { AdaptiveVoiceActivityDetector } from './voiceActivityDetector'
import { matchWakeWord } from './wakeWordRuntime'
import { ApiError } from './api'
import { isImmediatePrivacyStop } from './deviceCommandReceipt'
import {registeredWakePrefix,isExactStandbyPrivacyStop} from './nativeActivationAudio'

type Snapshot = { capture: ProtectedCaptureOwner; word: string; rate: number }
type RetryWindow={snapshot:Snapshot;wav:string;audioToken:unknown;expiresAt:number}
type Options = {
  capture: () => ProtectedCaptureOwner | null
  eligible: () => boolean
  word: () => string | undefined
  recognize: (wav: string, signal: AbortSignal, clientActionId: ReturnType<Crypto['randomUUID']>) => Promise<StandbyVoiceInput>
  onWake: () => void
  activationToken?:()=>unknown
  onWakeAudio?:(token:unknown)=>void
  onPrivacyStop?: () => void
  onUnavailable?: (reason:'timeout'|'unavailable') => void
  now?: () => number
}
/** Idle activation only; no lease is fabricated and no transcript is submitted. */
export class LocalStandbyWakeControl {
  private readonly now: () => number
  private detector = new AdaptiveVoiceActivityDetector()
  private snapshot: Snapshot | null = null
  private chunks: Float32Array[] = []
  private samples = 0
  private received = 0
  private sentAtSample = 0
  private frameAt = 0
  private request: AbortController | null = null
  private epoch = 0
  private windowStarts: number[] = []
  private lastVoiceSample = 0
  private busyWindow:RetryWindow|null=null
  private lastAt = -Infinity
  private disposed = false
  private timedOut = false
  constructor(private readonly options: Options) { this.now = options.now ?? (() => performance.now()) }
  private current(rate: number): Snapshot | null {
    let capture: ProtectedCaptureOwner | null, word: string | undefined
    try { capture = this.options.capture(); word = this.options.word() } catch { return null }
    if (this.timedOut || !this.options.eligible() || !capture || capture.inputKind !== 'realtime'
      || capture.localTrack.readyState !== 'live' || !capture.localTrack.enabled
      || capture.cloudTrack.readyState !== 'live' || capture.cloudTrack.enabled
      || typeof word !== 'string' || !/^[a-zA-Z0-9\u3400-\u9fff]{2,12}$/.test(word) || /^\d+$/.test(word)) return null
    return { capture, word, rate }
  }
  private same(a: Snapshot, b: Snapshot | null): boolean {
    return !!b && a.word === b.word && a.rate === b.rate && a.capture.sessionId === b.capture.sessionId
      && a.capture.childId === b.capture.childId && a.capture.localTrack === b.capture.localTrack
      && a.capture.inputKind === 'realtime' && b.capture.inputKind === 'realtime'
      && a.capture.cloudTrack === b.capture.cloudTrack
  }
  private clearFrames(): void {
    for (const frame of this.chunks) frame.fill(0)
    this.chunks = []; this.samples = 0; this.received = 0; this.sentAtSample = 0; this.frameAt = 0
    this.detector.reset();this.lastVoiceSample=0;this.busyWindow=null
  }
  private retire(): void {
    this.epoch++; this.request?.abort(); this.request = null; this.snapshot = null; this.clearFrames()
  }
  processFrame(frame: Float32Array, rate: number): void {
    if (this.disposed || !frame.length || frame.length > 8192 || !Number.isFinite(rate) || rate < 8000 || rate > 96000) return
    const current = this.current(rate), now = this.now()
    if (!current) { this.retire(); return }
    if (!this.snapshot || !this.same(this.snapshot, current)) { this.retire(); this.snapshot = current }
    if (this.frameAt && now - this.frameAt > 500) this.clearFrames()
    this.frameAt = now
    const limit = Math.floor(rate * 2.4), copy = frame.slice()
    this.chunks.push(copy); this.samples += copy.length; this.received += copy.length
    let energy = 0
    for (const value of frame) energy += value * value
    const activity = this.detector.observe(Math.sqrt(energy / frame.length), frame.length / rate * 1000)
    if(activity.voiced&&activity.speechDetected)this.lastVoiceSample=this.received
    while(this.windowStarts.length&&now-this.windowStarts[0]>=512000)this.windowStarts.shift()
    while (this.samples > limit) {
      const first = this.chunks[0], drop = Math.min(first.length, this.samples - limit)
      if (drop === first.length) this.chunks.shift(); else this.chunks[0] = first.slice(drop)
      first.fill(0); this.samples -= drop
    }
    if(this.busyWindow&&(now>=this.busyWindow.expiresAt||!this.same(this.busyWindow.snapshot,current)))this.busyWindow=null
    if (this.request || this.samples < limit || (!(activity.speechDetected&&this.lastVoiceSample>this.received-limit)&&!this.busyWindow) || this.windowStarts.length >= 512
      || now - this.lastAt < 750 || this.sentAtSample > 0 && this.received - this.sentAtSample < rate) return
    this.windowStarts.push(now); this.lastAt = now; this.sentAtSample = this.received
    const retained=this.busyWindow;this.busyWindow=null
    const window=retained??{snapshot:this.snapshot!,wav:encodeControlWav(this.chunks,rate),audioToken:this.options.activationToken?.(),expiresAt:now+5000}
    // A fresh window consumes only prior voice evidence. A BUSY retry has not
    // decoded its original window and must not erase newer pending speech.
    if(!retained)this.detector.reset()
    void this.dispatch(window)
  }
  private async dispatch(window:RetryWindow): Promise<void> {
    const {snapshot,wav,audioToken}=window
    const request = new AbortController(), epoch = ++this.epoch, clientActionId = crypto.randomUUID()
    this.request = request
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<null>(resolve => { timer = setTimeout(() => {
      request.abort(); if (this.request === request && this.epoch === epoch) {
        this.request = null; this.epoch++; this.timedOut = true; this.clearFrames()
        this.options.onUnavailable?.('timeout')
      }
      resolve(null)
    }, 5000) })
    try {
      const result = await Promise.race([this.options.recognize(wav, request.signal, clientActionId), timeout])
      if (!result || this.disposed || request.signal.aborted || this.request !== request || epoch !== this.epoch
        || !this.same(snapshot, this.current(snapshot.rate)) || result.client_action_id !== clientActionId
        || result.status !== 'completed' || result.cloud_used !== false || result.persisted !== false) return
      const prefix=registeredWakePrefix(result.transcript,snapshot.word)
      if(isImmediatePrivacyStop(result.transcript)||isExactStandbyPrivacyStop(result.transcript)
        ||prefix.matched&&isExactStandbyPrivacyStop(prefix.command)){
        this.retire();this.options.onPrivacyStop?.();return
      }
      if(!prefix.matched)return
      this.retire()
      if(prefix.command.trim())this.options.onWakeAudio?.(audioToken)
      else this.options.onWake()

    } catch (cause) {
      // A busy original decoder is still running. Wait for a later bounded
      // window; never claim abort stopped native work or queue another worker.
      if(cause instanceof ApiError&&cause.code==='LOCAL_ASR_BUSY'){
        if(!this.disposed&&!request.signal.aborted&&this.request===request&&epoch===this.epoch&&this.same(snapshot,this.current(snapshot.rate)))this.busyWindow=this.now()<window.expiresAt?window:null
        return
      }
      if(cause instanceof ApiError&&['ASR_NO_HUMAN_SPEECH','ASR_NO_SPEECH','ASR_NO_MEANINGFUL_SPEECH'].includes(cause.code??''))return
      if (!this.disposed&&!request.signal.aborted&&this.request===request&&epoch===this.epoch&&this.same(snapshot, this.current(snapshot.rate))) {
        this.timedOut = true; this.clearFrames(); this.options.onUnavailable?.('unavailable')
      }
    }
    finally { if (timer !== undefined) clearTimeout(timer); if (this.request === request) this.request = null }
  }
  /** Called only after the owning transport has been retired. It invalidates
   * the old waiter, never claims native cancellation, and preserves quota. */
  resumeAfterTransportRetired():boolean{
    if(this.disposed)return false
    this.retire();this.timedOut=false;return true
  }
  dispose(): void { this.disposed = true; this.retire() }
}
