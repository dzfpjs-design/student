import { microphoneEchoMode, type MicrophoneEchoMode } from './microphoneEchoCancellation'
import { getVoiceCaptureOwner } from './voiceCaptureCoordinator'
import { getCompanionVoiceState } from './companionVoice'

export const LISTEN_READY_GUIDANCE = '听到提示音完全结束，稍停一下再说。'
export type ListenReadyCapture = { owner: string; track: MediaStreamTrack; epoch: symbol; rejectUtterance: () => void }
export type ListenReadyScope = { sessionId: string; childId: string; prompt: object; endedAt: number;
  capture: ListenReadyCapture; sourceStamp: object; isCurrent: () => boolean; onReady?: () => void; needsRepeat?: () => boolean; onRepeatGiven?: () => void }
export type ListenReadyAttempt = { key: string; deadline: number; current: () => boolean; childId:string;
  sessionId:string;sourceStamp:object;captureEpoch:symbol;guidanceRequired:boolean;repeatRequired:boolean;notice?:Readonly<ListenReadyNoticeEnd> }
export type ListenReadyNoticeEnd = { naturalEnd:true;endedAt:number;sessionId:string;leaseId:string;leaseGeneration:number;leaseKind:'local_sapi'|'local_pcm' }
type Playback = { guidance: (attempt: ListenReadyAttempt, sessionId: string) => Promise<ListenReadyNoticeEnd|false>;
  tone: (attempt: ListenReadyAttempt, sessionId: string) => Promise<boolean>; cancel: (key: string) => void }
type Phase = 'quiet' | 'guidance' | 'guidance_quiet' | 'tone' | 'after_tone'
type Cue = { scope: ListenReadyScope; mode: MicrophoneEchoMode; phase: Phase; key: string; deadline: number;
  notice?:Readonly<ListenReadyNoticeEnd>; forceRepeat?:boolean; blockedUntil: number; quietMs: number; frameAt: number; epoch: symbol; timer: ReturnType<typeof setTimeout> }
const cues = new Map<string, Cue>()
const guidanceGiven = new Map<string, MicrophoneEchoMode>()
const used = new WeakMap<object, Set<string>>()
const recoveryUsed = new WeakMap<object, Set<string>>()
let playback: Playback | undefined
let sequence = 0
const sourceCurrent=(scope:ListenReadyScope):boolean=>{try{return scope.isCurrent()===true}catch{return false}}
const ownerKey = (scope: ListenReadyScope) => `${scope.sessionId}\0${scope.childId}\0${scope.capture.owner}`

/** Readiness feedback only. This metadata never authorizes ASR or retains audio. */
export function configureListenReadyPlayback(value: Playback): void { playback = value }
function current(cue: Cue): boolean {
  try { return cues.get(cue.scope.sessionId) === cue && Date.now() < cue.deadline && cue.scope.isCurrent()
    && getVoiceCaptureOwner() === cue.scope.capture.owner && cue.scope.capture.track.readyState === 'live'
    && cue.scope.capture.track.enabled === true && microphoneEchoMode(cue.scope.capture.track) === cue.mode }
  catch { return false }
}
function finish(cue: Cue, ready: boolean): void {
  if (cues.get(cue.scope.sessionId) !== cue) return
  cues.delete(cue.scope.sessionId); clearTimeout(cue.timer)
  if (!ready) {
    try { cue.scope.capture.rejectUtterance() } catch { /* Metadata cleanup cannot strand owned playback. */ }
    try { playback?.cancel(cue.key) } catch { /* Adapter retains its independent settled cleanup. */ }
  } else {try {cue.scope.onReady?.()}catch {/* Feedback callbacks cannot retain a capture hold. */}}
}
export function cancelListenReady(sessionId?: string, owner?: string, capture?: {track:MediaStreamTrack;epoch:symbol}): void {
  for (const cue of [...cues.values()]) if ((!sessionId || cue.scope.sessionId === sessionId)
    && (!owner || cue.scope.capture.owner === owner)
    &&(!capture||(cue.scope.capture.track===capture.track&&cue.scope.capture.epoch===capture.epoch))) finish(cue, false)
}
export function clearListenReadyGuidance(sessionId: string, childId: string | undefined, owner: string): void {
  cancelListenReady(sessionId, owner)
  guidanceGiven.delete(`${sessionId}\0${childId ?? ''}\0${owner}`)
}
export function listenReadyPending(sessionId: string, childId: string, owner: string, capture?: {track:MediaStreamTrack;epoch:symbol}): boolean {
  const cue = cues.get(sessionId)
  return !!cue && cue.scope.childId === childId && cue.scope.capture.owner === owner
    &&(!capture||(cue.scope.capture.track===capture.track&&cue.scope.capture.epoch===capture.epoch))
}
export function requestListenReady(scope: ListenReadyScope): void {
  const mode = microphoneEchoMode(scope.capture.track), identity = ownerKey(scope)
  if (mode === 'all') { guidanceGiven.delete(identity); return }
  if (!playback || !sourceCurrent(scope) || getVoiceCaptureOwner() !== scope.capture.owner
    || scope.capture.track.readyState !== 'live' || !scope.capture.track.enabled
    || !Number.isFinite(scope.endedAt) || scope.endedAt > Date.now() || Date.now() - scope.endedAt >= 8000) return
  const previous = used.get(scope.prompt) ?? new Set<string>()
  if (previous.has(identity)) return
  cancelListenReady(scope.sessionId)
  previous.add(identity); used.set(scope.prompt, previous)
  const deadline = scope.endedAt + 8000
  const cue: Cue = { scope, mode, phase: 'quiet', key: `listen-ready-${++sequence}`, deadline,
    blockedUntil: scope.endedAt + 850, quietMs: 0, frameAt: 0, epoch: scope.capture.epoch,
    timer: setTimeout(() => finish(cue, false), deadline - Date.now()) }
  cues.set(scope.sessionId, cue)
}
/** Only the recorder's continuous 1050ms real-PCM quiet callback calls this.
 * It creates independent feedback metadata; no original capture/ASR grant is renewed. */
export function requestListenReadyRecovery(scope: ListenReadyScope): void {
  const mode=microphoneEchoMode(scope.capture.track),identity=ownerKey(scope)
  if(mode==='all'||!playback||!sourceCurrent(scope)||getVoiceCaptureOwner()!==scope.capture.owner
    ||scope.capture.track.readyState!=='live'||scope.capture.track.enabled!==true)return
  const prior=recoveryUsed.get(scope.prompt)??new Set<string>()
  if(prior.has(identity))return
  prior.add(identity);recoveryUsed.set(scope.prompt,prior);cancelListenReady(scope.sessionId)
  const cue:Cue={scope,mode,phase:'quiet',key:`listen-ready-${++sequence}`,deadline:Date.now()+12000,
    blockedUntil:Date.now(),quietMs:0,frameAt:0,epoch:scope.capture.epoch,forceRepeat:true,
    timer:setTimeout(()=>finish(cue,false),12000)}
  cues.set(scope.sessionId,cue);play(cue,'guidance')
}
function play(cue: Cue, kind: 'guidance' | 'tone'): void {
  if (!current(cue) || !playback) { finish(cue, false); return }
  cue.phase = kind; cue.quietMs = 0; cue.frameAt = 0
  if(kind==='guidance'){clearTimeout(cue.timer);cue.deadline=Date.now()+12000;cue.timer=setTimeout(()=>finish(cue,false),12000)}
  const attempt:ListenReadyAttempt = { key: cue.key, deadline: cue.deadline, current: () => current(cue),childId:cue.scope.childId,
    sessionId:cue.scope.sessionId,sourceStamp:cue.scope.sourceStamp,captureEpoch:cue.epoch,
    guidanceRequired:guidanceGiven.get(ownerKey(cue.scope))!==cue.mode,repeatRequired:cue.forceRepeat===true||cue.scope.needsRepeat?.()===true,...(cue.notice?{notice:cue.notice}:{}) }
  void playback[kind](attempt, cue.scope.sessionId).then(naturalEnd => {
    if (!current(cue) || getCompanionVoiceState() !== 'idle') { finish(cue, false); return }
    cue.quietMs = 0; cue.frameAt = 0
    if (kind === 'guidance') {
      if(!naturalEnd||typeof naturalEnd!=='object'||naturalEnd.naturalEnd!==true||naturalEnd.sessionId!==cue.scope.sessionId
        ||!naturalEnd.leaseId||!Number.isSafeInteger(naturalEnd.leaseGeneration)||naturalEnd.leaseGeneration<=0
        ||!['local_sapi','local_pcm'].includes(naturalEnd.leaseKind)||!Number.isFinite(naturalEnd.endedAt)
        ||naturalEnd.endedAt>Date.now()||naturalEnd.endedAt+8000<=Date.now()){finish(cue,false);return}
      cue.notice=Object.freeze({...naturalEnd})
      if(attempt.guidanceRequired)guidanceGiven.set(ownerKey(cue.scope), cue.mode)
      if(attempt.repeatRequired)cue.scope.onRepeatGiven?.()
      clearTimeout(cue.timer);cue.deadline=naturalEnd.endedAt+8000;cue.timer=setTimeout(()=>finish(cue,false),cue.deadline-Date.now())
      cue.phase = 'guidance_quiet'; cue.blockedUntil = naturalEnd.endedAt + 850
    } else {if(naturalEnd!==true){finish(cue,false);return}cue.phase = 'after_tone';cue.blockedUntil=Date.now()}
  }).catch(() => finish(cue, false))
}
/** Called only with actual current processor frames; elapsed time alone is not quiet. */
export function observeListenReady(sessionId: string, childId: string, capture: { owner: string; track: MediaStreamTrack; epoch: symbol },
  voiced: boolean, frameMs: number, now: number): { blocked: boolean; reject: boolean } {
  const cue = cues.get(sessionId)
  if (!cue) return { blocked: false, reject: false }
  if(cue.scope.childId!==childId||cue.scope.capture.owner!==capture.owner||cue.scope.capture.track!==capture.track||cue.epoch!==capture.epoch)
    return {blocked:true,reject:true}
  if (!current(cue) || !Number.isFinite(frameMs) || frameMs <= 0 || frameMs > 500) {
    finish(cue, false); return { blocked: true, reject: true }
  }
  if (cue.frameAt && now - cue.frameAt > 500) cue.quietMs = 0
  cue.frameAt = now
  if (voiced || getCompanionVoiceState() !== 'idle' || now < cue.blockedUntil) cue.quietMs = 0
  else cue.quietMs += Math.min(frameMs,Math.max(0,now-cue.blockedUntil))
  if (cue.phase === 'after_tone' && voiced) {
    finish(cue, false); return { blocked: true, reject: true }
  }
  if (cue.phase === 'after_tone' && cue.quietMs >= 160) {
    finish(cue, true); return { blocked: false, reject: false }
  }
  if ((cue.phase === 'quiet' || cue.phase === 'guidance_quiet') && cue.quietMs >= 1050) {
    play(cue, cue.phase==='guidance_quiet'||(guidanceGiven.get(ownerKey(cue.scope)) === cue.mode && cue.scope.needsRepeat?.()!==true) ? 'tone' : 'guidance')
  }
  return { blocked: true, reject: voiced }
}
