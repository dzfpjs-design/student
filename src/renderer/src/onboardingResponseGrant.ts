import { requestListenReady, requestListenReadyRecovery, cancelListenReady } from './listenReadyCue'
import { getCompanionPlaybackLease, getCompanionVoiceState, subscribeCompanionVoice } from './companionVoice'
import { getVoiceCaptureOwner, subscribeVoiceCapture } from './voiceCaptureCoordinator'
import { microphoneEchoMode, type MicrophoneEchoPolicy } from './microphoneEchoCancellation'

type Scope = { sessionId: string; childId: string; phase: 'naming' | 'active'; step: string | null;
  prompt: object; promptText: string; epoch: number; isCurrent: () => boolean }
type Capture = { owner: string; track: MediaStreamTrack; epoch: symbol; echoPolicy: MicrophoneEchoPolicy; rejectUtterance?: () => void; onCueReady?: () => void; needsRepeat?: () => boolean; onRepeatGiven?: () => void }
const captures = new Map<string, { childId: string; capture: Capture; isCurrent: () => boolean }>()
const current = new Map<string, OnboardingResponseGrant>()
// Current prompt provenance for feedback only; never an early-audio grant.
const feedbacks = new Map<string, OnboardingResponseGrant>()

/** Renderer-memory response permission. It grants no question, scoring, retention, or cloud permission. */
export class OnboardingResponseGrant {
  private status: 'waiting' | 'playing' | 'ready' | 'claimed' | 'revoked' = 'waiting'
  private playback: { id: string; generation: number } | null = null
  private capture: Capture | null = null
  private readyAt = 0
  private expiresAt = 0
  private expiry: ReturnType<typeof setTimeout> | undefined
  private submissionStarted = false
  private voiceUnsubscribe: (() => void) | undefined
  private captureUnsubscribe: (() => void) | undefined
  private revoked = new Set<() => void>()
  constructor(private readonly scope: Scope) {}
  private validScope(): boolean {
    return !!this.scope.sessionId && !!this.scope.childId && !!this.scope.prompt && typeof this.scope.prompt === 'object'
      && !!this.scope.promptText.trim() && Number.isSafeInteger(this.scope.epoch) && this.scope.epoch > 0
      && ((this.scope.phase === 'naming' && this.scope.step === null)
        || (this.scope.phase === 'active' && ['wake_practice', 'study_practice', 'document_practice', 'finish_practice'].includes(this.scope.step ?? '')))
  }
  private owned(): boolean {
    if (!this.validScope()) return false
    try { return this.status !== 'revoked' && current.get(this.scope.sessionId) === this && this.scope.isCurrent() === true }
    catch { return false }
  }
  started(): void {
    const lease = getCompanionPlaybackLease()
    if (!this.owned() || this.status !== 'waiting' || !lease || lease.sessionId !== this.scope.sessionId
      || !['local_sapi', 'managed', 'local_pcm'].includes(lease.kind)
      || (lease.kind === 'local_sapi' && lease.text !== this.scope.promptText)) { this.revoke(); return }
    this.playback = { id: lease.id, generation: lease.generation }; this.status = 'playing'
    this.voiceUnsubscribe = subscribeCompanionVoice(state => {
      if (state === 'preparing' || state === 'error' || state === 'interrupted') this.revoke()
      if (state === 'speaking') {
        const next = getCompanionPlaybackLease()
        if (next?.id !== this.playback?.id || next?.generation !== this.playback?.generation) this.revoke()
      }
    })
    this.captureUnsubscribe = subscribeVoiceCapture(() => {
      if (this.capture && getVoiceCaptureOwner() !== this.capture.owner) this.revoke()
    })
  }
  /** Called only by the same owned playback's natural completion callback. */
  naturalEnd(): void {
    if (!this.owned() || this.status !== 'playing' || !this.playback || getCompanionVoiceState() !== 'idle') { this.revoke(); return }
    const actual = captures.get(this.scope.sessionId)
    this.capture = actual?.childId === this.scope.childId && actual.isCurrent() ? actual.capture : null
    this.readyAt = Date.now(); feedbacks.set(this.scope.sessionId,this); this.status = 'ready'; this.setDeadline(8000)
    const capture=this.capture
    if(actual&&capture?.rejectUtterance)requestListenReady({sessionId:this.scope.sessionId,childId:this.scope.childId,
      prompt:this.scope.prompt,endedAt:this.readyAt,capture:{...capture,rejectUtterance:capture.rejectUtterance},onReady:capture.onCueReady,needsRepeat:capture.needsRepeat,onRepeatGiven:capture.onRepeatGiven,sourceStamp:this.scope,
      isCurrent:()=>this.validScope()&&this.scope.isCurrent()&&(!current.has(this.scope.sessionId)||current.get(this.scope.sessionId)===this)
        &&captures.get(this.scope.sessionId)===actual&&actual.isCurrent()})
  }
  recoverListenReady(value:Pick<Capture,'owner'|'track'|'epoch'>):void {
    const actual=captures.get(this.scope.sessionId)
    if(!this.readyAt||!this.validScope()||feedbacks.get(this.scope.sessionId)!==this||!this.scope.isCurrent()
      ||!actual||actual.childId!==this.scope.childId||!actual.isCurrent()||actual.capture.track!==value.track
      ||actual.capture.epoch!==value.epoch||!actual.capture.rejectUtterance)return
    requestListenReadyRecovery({sessionId:this.scope.sessionId,childId:this.scope.childId,prompt:this.scope.prompt,endedAt:this.readyAt,
      capture:{...actual.capture,rejectUtterance:actual.capture.rejectUtterance},sourceStamp:this.scope,onReady:actual.capture.onCueReady,onRepeatGiven:actual.capture.onRepeatGiven,
      isCurrent:()=>this.validScope()&&feedbacks.get(this.scope.sessionId)===this&&this.scope.isCurrent()
        &&captures.get(this.scope.sessionId)===actual&&actual.isCurrent()})
  }
  private setDeadline(milliseconds: number): void {
    clearTimeout(this.expiry); this.expiresAt = Date.now() + milliseconds; this.expiry = setTimeout(() => this.revoke(), milliseconds)
  }
  private scopeMatches(sessionId: string, childId: string): boolean {
    return this.owned() && this.scope.sessionId === sessionId && this.scope.childId === childId
      && ['ready', 'claimed'].includes(this.status) && Date.now() >= this.readyAt && Date.now() < this.expiresAt
      && getCompanionVoiceState() === 'idle'
  }
  private captureMatches(value: Capture): boolean {
    const actual = captures.get(this.scope.sessionId)
    return !!this.capture && actual?.childId === this.scope.childId && actual.isCurrent()
      && actual.capture.track === value.track && actual.capture.epoch === value.epoch
      && getVoiceCaptureOwner() === value.owner && value.track.readyState === 'live' && value.track.enabled === true
      && value.echoPolicy.negotiation === 'accepted' && microphoneEchoMode(value.track) === 'all'
      && (!this.capture || (this.capture.owner === value.owner && this.capture.track === value.track && this.capture.epoch === value.epoch))
  }
  canBuffer(sessionId: string, childId: string, value: Capture): boolean {
    return this.scopeMatches(sessionId, childId) && this.captureMatches(value)
  }
  responsePending(sessionId: string, childId: string): boolean { return this.scopeMatches(sessionId, childId) && this.status === 'ready' }
  claim(sessionId: string, childId: string, value: Capture): boolean {
    if (this.status !== 'ready' || !this.canBuffer(sessionId, childId, value)) return false
    this.capture = value; this.status = 'claimed'; this.setDeadline(12000); return true
  }
  capturedCurrent(sessionId: string, childId: string, value: Capture): boolean {
    return this.status === 'claimed' && this.canBuffer(sessionId, childId, value)
  }
  async beforeSubmit(signal: AbortSignal): Promise<boolean> {
    if (this.submissionStarted || signal.aborted || this.status !== 'claimed'
      || !this.scopeMatches(this.scope.sessionId, this.scope.childId)) return false
    // One complete capture gets one bounded local ASR/handoff period, never a renewed onset grant.
    this.submissionStarted = true; this.setDeadline(30000); return true
  }
  subscribeRevoked(callback: () => void): () => void {
    if (!this.owned()) { callback(); return () => {} }
    this.revoked.add(callback); return () => this.revoked.delete(callback)
  }
  revoke(): void {
    if (this.status === 'revoked') return
    this.status = 'revoked'; clearTimeout(this.expiry); this.voiceUnsubscribe?.(); this.captureUnsubscribe?.()
    if (current.get(this.scope.sessionId) === this) current.delete(this.scope.sessionId)
    this.capture = null; this.playback = null
    for (const callback of this.revoked) { try { callback() } catch { /* One consumer must not strand other owned cleanup. */ } }
    this.revoked.clear()
  }
}
export function beginOnboardingResponse(scope: Scope): OnboardingResponseGrant {
  cancelListenReady(scope.sessionId);feedbacks.delete(scope.sessionId)
  current.get(scope.sessionId)?.revoke()
  const grant = new OnboardingResponseGrant(scope); current.set(scope.sessionId, grant); return grant
}
export function onboardingResponseGrant(sessionId: string, childId: string): OnboardingResponseGrant | null {
  const grant = current.get(sessionId)
  return grant?.responsePending(sessionId, childId) ? grant : null
}
export function revokeOnboardingResponses(): void { feedbacks.clear();for (const grant of [...current.values()]) grant.revoke() }

/** Actual recorder registration, not a supplied question or diagnostic grant. */
export function registerOnboardingCapture(sessionId: string, childId: string, capture: Capture, isCurrent: () => boolean): () => void {
  if (captures.has(sessionId)) {const old=captures.get(sessionId)!;cancelListenReady(sessionId,old.capture.owner,old.capture);current.get(sessionId)?.revoke()}
  const value = { childId, capture, isCurrent }
  captures.set(sessionId, value)
  return () => {
    if (captures.get(sessionId) !== value) return
    cancelListenReady(sessionId,capture.owner,capture)
    captures.delete(sessionId)
    current.get(sessionId)?.revoke()
  }
}

export function recoverOnboardingListenReady(sessionId:string,childId:string,capture:Pick<Capture,'owner'|'track'|'epoch'>):void {
 const grant=feedbacks.get(sessionId);if(captures.get(sessionId)?.childId===childId)grant?.recoverListenReady(capture)
}
