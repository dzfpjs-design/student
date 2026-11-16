import { requestListenReady, requestListenReadyRecovery, cancelListenReady } from './listenReadyCue'
import { localApi } from './api'
import { getCompanionPlaybackLease, getCompanionVoiceState, subscribeCompanionVoice, type CompanionSpeechOptions } from './companionVoice'
import { getVoiceCaptureOwner, subscribeVoiceCapture } from './voiceCaptureCoordinator'
import { microphoneEchoMode, type MicrophoneEchoPolicy } from './microphoneEchoCancellation'
import { voiceCaptureReference, voiceCaptureScopeStamp, subscribeVoiceCaptureContext, type VoiceCaptureReference } from './voiceCaptureContext'

export type OrdinaryResponseBinding = { version: 'ordinary_prompt_response_v1'; session_state: string;
  source_agent_turn_id?: string; source_tutor_turn_id?: string; source_tutor_voice_playback_id?:string }
type Capture = { owner: string; track: MediaStreamTrack; epoch: symbol; echoPolicy: MicrophoneEchoPolicy; rejectUtterance?: () => void; onCueReady?: () => void; needsRepeat?: () => boolean; onRepeatGiven?: () => void }
type Session = { sessionId: string; childId: string; state: string }
type Scope = Session & { prompt: object; text: string; isCurrent: () => boolean;
  sourceAgentTurnId?: string; sourceTutorTurnId?: string; deliveryPending?: boolean }
const sessions = new Map<string, Session>()
const captures = new Map<string, { childId: string; capture: Capture; isCurrent: () => boolean }>()
const grants = new Map<string, OrdinaryResponseGrant>()
// Current prompt provenance for feedback only; never an early-audio grant.
const feedbacks = new Map<string, OrdinaryResponseGrant>()
export type OrdinaryPromptSourceFact = Readonly<{agentTurnId:string|null;identity:object}>
const promptSourceFacts = new WeakMap<OrdinaryResponseGrant,OrdinaryPromptSourceFact>()
const states = new Set(['check_in','classroom_recall','homework_capture','plan_review','study','break','reflection'])
export const REPEAT_RESPONSE = '这次没听完整。请先停一下，再说一遍。'

/** A local, transient response lease. Server capture/source validation remains authoritative. */
export class OrdinaryResponseGrant {
  private phase: 'waiting'|'playing'|'ready'|'claimed'|'revoked' = 'waiting'
  private lease: {id:string;generation:number}|null = null
  private capture: Capture|null = null
  private readonly session: Session|undefined
  private readonly reference: VoiceCaptureReference
  private readonly stamp: ReturnType<typeof voiceCaptureScopeStamp>
  private endedAt = 0
  private expires = 0
  private timer: ReturnType<typeof setTimeout>|undefined
  private submissionStarted=false
  private readonly cleanup: Array<()=>void> = []
  private readonly revoked = new Set<()=>void>()
  private accepted=false
  private acceptedCapture:Capture|null=null
  private deliveryReady: boolean
  private deliveryWaiters = new Set<()=>void>()
  readonly binding: OrdinaryResponseBinding
  constructor(private readonly scope: Scope) {
    this.session=sessions.get(scope.sessionId)
    this.reference=voiceCaptureReference(scope.sessionId,scope.childId)
    this.stamp=voiceCaptureScopeStamp(scope.sessionId,scope.childId)
    this.deliveryReady=!scope.deliveryPending
    this.binding={version:'ordinary_prompt_response_v1',session_state:scope.state,
      ...(scope.sourceAgentTurnId?{source_agent_turn_id:scope.sourceAgentTurnId}:{}),
      ...(scope.sourceTutorTurnId?{source_tutor_turn_id:scope.sourceTutorTurnId}:{})}
    promptSourceFacts.set(this,Object.freeze({agentTurnId:scope.sourceAgentTurnId??null,identity:Object.freeze({})}))
    this.cleanup.push(subscribeVoiceCaptureContext(()=>{if(!this.owned())this.revoke()}))
  }
  private currentScope(): boolean {
    const stamp=voiceCaptureScopeStamp(this.scope.sessionId,this.scope.childId)
    try {return this.session!==undefined&&sessions.get(this.scope.sessionId)===this.session
      &&this.session.childId===this.scope.childId&&this.session.state===this.scope.state&&states.has(this.scope.state)
      &&this.scope.isCurrent()===true&&!!this.scope.prompt&&typeof this.scope.prompt==='object'&&!!this.scope.text.trim()
      &&!this.reference.pending_evaluation_turn_id&&!this.reference.local_recovery_only
      &&stamp.value===this.stamp.value&&stamp.documentOwner===this.stamp.documentOwner
      &&JSON.stringify(voiceCaptureReference(this.scope.sessionId,this.scope.childId))===JSON.stringify(this.reference)}
    catch{return false}
  }
  private owned():boolean {return this.phase!=='revoked'&&grants.get(this.scope.sessionId)===this&&this.currentScope()}
  playbackCurrent():boolean {return this.owned()}
  /** Frozen prompt provenance only; it grants no response or capture authority. */
  promptCurrent():boolean {return this.currentScope()}
  playingCurrent():boolean {return this.phase==='playing'&&this.owned()}
  playingScope(sessionId:string,childId:string):boolean{return this.playingCurrent()&&sessionId===this.scope.sessionId&&childId===this.scope.childId}
  started():void {
    const lease=getCompanionPlaybackLease()
    if(!this.owned()||this.phase!=='waiting'||!lease||lease.sessionId!==this.scope.sessionId
      ||!['local_sapi','managed','local_pcm'].includes(lease.kind)
      ||(lease.kind==='local_sapi'&&lease.text!==this.scope.text)){this.revoke();return}
    this.lease={id:lease.id,generation:lease.generation};this.phase='playing'
    this.cleanup.push(subscribeCompanionVoice(state=>{
      const current=getCompanionPlaybackLease()
      if(['preparing','error','interrupted'].includes(state)||(state==='speaking'
        &&(current?.id!==this.lease?.id||current?.generation!==this.lease?.generation)))this.revoke()
    }),subscribeVoiceCapture(()=>{if(this.capture&&getVoiceCaptureOwner()!==this.capture.owner)this.revoke()}))
  }
  naturalEnd():void {
    if(!this.owned()||this.phase!=='playing'||!this.lease||getCompanionVoiceState()!=='idle'){this.revoke();return}
    const current=captures.get(this.scope.sessionId)
    this.capture=current?.childId===this.scope.childId&&current.isCurrent()?current.capture:null
    this.endedAt=Date.now();feedbacks.set(this.scope.sessionId,this);this.phase='ready';this.setDeadline(8000);this.publishListenReady()
  }
  private setDeadline(milliseconds:number):void {
    clearTimeout(this.timer);this.expires=Date.now()+milliseconds;this.timer=setTimeout(()=>this.revoke(),milliseconds)
  }
  private publishListenReady():void {
    const actual=captures.get(this.scope.sessionId),capture=this.capture
    if(!this.deliveryReady||!this.endedAt||!actual||!capture?.rejectUtterance)return
    requestListenReady({sessionId:this.scope.sessionId,childId:this.scope.childId,prompt:this.scope.prompt,endedAt:this.endedAt,
      capture:{...capture,rejectUtterance:capture.rejectUtterance},onReady:capture.onCueReady,needsRepeat:capture.needsRepeat,onRepeatGiven:capture.onRepeatGiven,sourceStamp:this.stamp,
      isCurrent:()=>this.currentScope()&&(!grants.has(this.scope.sessionId)||grants.get(this.scope.sessionId)===this)
        &&captures.get(this.scope.sessionId)===actual&&actual.isCurrent()})
  }
  recoverListenReady(value:Pick<Capture,'owner'|'track'|'epoch'>):void {
    const actual=captures.get(this.scope.sessionId)
    if(!this.endedAt||!this.deliveryReady||!this.currentScope()||feedbacks.get(this.scope.sessionId)!==this
      ||!actual||actual.childId!==this.scope.childId||!actual.isCurrent()||actual.capture.track!==value.track
      ||actual.capture.epoch!==value.epoch||!actual.capture.rejectUtterance)return
    requestListenReadyRecovery({sessionId:this.scope.sessionId,childId:this.scope.childId,prompt:this.scope.prompt,endedAt:this.endedAt,
      capture:{...actual.capture,rejectUtterance:actual.capture.rejectUtterance},sourceStamp:this.stamp,onReady:actual.capture.onCueReady,onRepeatGiven:actual.capture.onRepeatGiven,
      isCurrent:()=>this.currentScope()&&feedbacks.get(this.scope.sessionId)===this&&captures.get(this.scope.sessionId)===actual&&actual.isCurrent()})
  }
  markAccepted():void {if(this.owned()&&this.phase==='claimed'){this.accepted=true;this.acceptedCapture=this.capture}}
  handoffCurrent():boolean {const actual=captures.get(this.scope.sessionId),capture=this.acceptedCapture;return this.accepted&&this.submissionStarted&&Date.now()<this.expires&&this.currentScope()
    &&(!grants.has(this.scope.sessionId)||grants.get(this.scope.sessionId)===this)&&!!capture&&actual?.childId===this.scope.childId&&actual.isCurrent()
    &&actual.capture.track===capture.track&&actual.capture.epoch===capture.epoch&&capture.track.readyState==='live'&&capture.track.enabled===true
    &&getVoiceCaptureOwner()===capture.owner&&microphoneEchoMode(capture.track)==='all'}
  wasAccepted():boolean {return this.accepted}
  attachTutorReceipt(id:string):void {if(this.phase!=='waiting'||!id)throw Error('ORDINARY_TUTOR_RECEIPT_INVALID');this.binding.source_tutor_voice_playback_id=id}
  confirmDelivery():void {if(!this.owned())return;this.deliveryReady=true;this.publishListenReady();for(const resolve of this.deliveryWaiters)resolve();this.deliveryWaiters.clear()}
  responsePending(sessionId:string,childId:string):boolean {return this.phase==='ready'&&this.current(sessionId,childId)}
  private current(sessionId:string,childId:string):boolean {return this.owned()&&sessionId===this.scope.sessionId&&childId===this.scope.childId
    &&['ready','claimed'].includes(this.phase)&&Date.now()<this.expires&&getCompanionVoiceState()==='idle'}
  canBuffer(sessionId:string,childId:string,value:Capture):boolean {
    const actual=captures.get(sessionId)
    return this.current(sessionId,childId)&&!!this.capture&&actual?.childId===childId&&actual.isCurrent()
      &&actual.capture.track===value.track&&actual.capture.epoch===value.epoch
      &&this.capture.owner===value.owner&&this.capture.track===value.track&&this.capture.epoch===value.epoch
      &&getVoiceCaptureOwner()===value.owner&&value.track.readyState==='live'&&value.track.enabled===true
      &&value.echoPolicy.negotiation==='accepted'&&microphoneEchoMode(value.track)==='all'
  }
  claim(sessionId:string,childId:string,value:Capture):boolean {
    if(this.phase!=='ready'||!this.canBuffer(sessionId,childId,value))return false
    // Claim once: the existing recorder permits a 12s capture cycle, independent of onset eligibility.
    this.phase='claimed';this.setDeadline(12000);return true
  }
  capturedCurrent(sessionId:string,childId:string,value:Capture):boolean {return this.phase==='claimed'&&this.canBuffer(sessionId,childId,value)}
  capturedReference():VoiceCaptureReference {return structuredClone(this.reference)}
  async beforeSubmit(signal:AbortSignal):Promise<boolean> {
    if(this.submissionStarted||signal.aborted||!this.current(this.scope.sessionId,this.scope.childId)||this.phase!=='claimed')return false
    // One completed capture gets one bounded local ACK/ASR/handoff period; retries never renew it.
    this.submissionStarted=true;this.setDeadline(30000)
    if(!this.deliveryReady&&this.owned()&&!signal.aborted)await new Promise<void>(resolve=>{
      const finish=()=>{this.deliveryWaiters.delete(finish);signal.removeEventListener('abort',finish);unsubscribe();resolve()}
      let unsubscribe=()=>{};unsubscribe=this.subscribeRevoked(finish);this.deliveryWaiters.add(finish);signal.addEventListener('abort',finish,{once:true})
      if(this.deliveryReady||signal.aborted||!this.owned())finish()
    })
    return this.deliveryReady&&!signal.aborted&&this.owned()&&this.phase==='claimed'&&Date.now()<this.expires
  }
  subscribeRevoked(callback:()=>void):()=>void {if(!this.owned()){callback();return()=>{}}this.revoked.add(callback);return()=>{this.revoked.delete(callback)}}
  revoke():void {
    if(this.phase==='revoked')return;this.phase='revoked';clearTimeout(this.timer)
    for(const dispose of this.cleanup.splice(0))dispose()
    if(grants.get(this.scope.sessionId)===this)grants.delete(this.scope.sessionId)
    this.capture=null;this.lease=null
    for(const fn of this.revoked){try{fn()}catch{/* Independent owned consumers must still close. */}}this.revoked.clear()
    for(const resolve of this.deliveryWaiters)resolve();this.deliveryWaiters.clear()
  }
}
export function registerOrdinarySession(value:Session):()=>void {
  const old=sessions.get(value.sessionId)
  if(old&&old.childId===value.childId&&old.state===value.state)return()=>{}
  cancelListenReady(value.sessionId);feedbacks.delete(value.sessionId);grants.get(value.sessionId)?.revoke();sessions.set(value.sessionId,value)
  return()=>{if(sessions.get(value.sessionId)===value){sessions.delete(value.sessionId);grants.get(value.sessionId)?.revoke()}}
}
export function beginOrdinaryResponse(scope:Scope):OrdinaryResponseGrant {cancelListenReady(scope.sessionId);feedbacks.delete(scope.sessionId);grants.get(scope.sessionId)?.revoke();const grant=new OrdinaryResponseGrant(scope);grants.set(scope.sessionId,grant);return grant}
export function ordinaryResponseGrant(sessionId:string,childId:string):OrdinaryResponseGrant|null {
  const grant=grants.get(sessionId);return grant?.responsePending(sessionId,childId)?grant:null
}
/** Current prompt provenance only, independent of response permission.
 * The existing feedback owner retains this fact after its fixed8s grant is
 * revoked. It still has to pass the original full prompt-current checks.
 * A source fact cannot claim, capture, submit, renew or revive that grant.
 * Its private empty identity only lets an old notice recognize replacement.
 */
export function ordinaryPromptSourceFact(sessionId:string,childId:string):OrdinaryPromptSourceFact|null {
  const session=sessions.get(sessionId),prompt=grants.get(sessionId)??feedbacks.get(sessionId)
  return session?.childId===childId&&prompt?.promptCurrent()?promptSourceFacts.get(prompt)??null:null
}
export function ordinaryPromptSourceAgent(sessionId:string,childId:string):string|null {
  return ordinaryPromptSourceFact(sessionId,childId)?.agentTurnId??null
}
/** Output provenance only; never a pre-natural-end response/capture grant. */
export function ordinaryPlayingPrompt(sessionId:string,childId:string):OrdinaryResponseGrant|null {
  const grant=grants.get(sessionId);return grant?.playingScope(sessionId,childId)?grant:null
}
export function revokeOrdinaryResponses():void {feedbacks.clear();for(const grant of [...grants.values()])grant.revoke()}
export function registerOrdinaryCapture(sessionId:string,childId:string,capture:Capture,isCurrent:()=>boolean):()=>void {
  if(captures.has(sessionId)){const old=captures.get(sessionId)!;cancelListenReady(sessionId,old.capture.owner,old.capture);grants.get(sessionId)?.revoke()}
  const entry={childId,capture,isCurrent};captures.set(sessionId,entry)
  return()=>{if(captures.get(sessionId)!==entry)return;captures.delete(sessionId);grants.get(sessionId)?.revoke()}
}
/** Dedicated capture protocols retain their existing playback and ASR authority. */
export function ordinaryResponseMayOwnPrompt(sessionId:string,childId:string,options:CompanionSpeechOptions={}):boolean {
  try {
    const reference=voiceCaptureReference(sessionId,childId)
    return reference.child_id===childId&&!options.pcmControlReference&&!reference.question_reference
      &&!reference.activity_reference&&!reference.task_choice_reference
      &&!reference.pending_evaluation_turn_id&&!reference.local_recovery_only
  }catch{return false}
}
export function ordinaryPromptPlayback(scope:Scope,options:CompanionSpeechOptions={},allowFallbackRebind=true):{grant:OrdinaryResponseGrant;options:CompanionSpeechOptions;started:()=>void} {
  let grant=beginOrdinaryResponse(scope),closed=false,ended=false,replacing=false,fallback=false
  const provenance=grant
  let actual:{id:string;generation:number;kind:string}|undefined
  let unsubscribe=()=>{}
  const promptCurrent=()=>!closed&&provenance.promptCurrent()&&(options.isCurrent?.()??true)
  const close=()=>{closed=true;unsubscribe();grant.revoke()}
  const watchGrant=()=>grant.subscribeRevoked(()=>{if(!replacing){closed=true;unsubscribe()}})
  watchGrant()
  // Retire the partial managed grant before fallback preparation. The same
  // speech attempt can bind a fresh grant only at its actual local start.
  if(!closed)unsubscribe=subscribeCompanionVoice(state=>{
    if(state!=='preparing'||!allowFallbackRebind||!actual||actual.kind!=='managed'||ended||fallback||!promptCurrent()||!grant.playingCurrent())return
    fallback=true;replacing=true;grant.revoke();replacing=false
  })
  const current=()=>promptCurrent()&&(grant.playbackCurrent()||(fallback&&!grants.has(scope.sessionId)))
  return {get grant(){return grant},started:()=>{
    const lease=getCompanionPlaybackLease()
    if(!current()||!lease){close();return}
    if(fallback){
      if(!actual||lease.id!==actual.id||lease.generation!==actual.generation||lease.kind!=='local_sapi'||lease.text!==scope.text){close();return}
      replacing=true;grant=beginOrdinaryResponse(scope);replacing=false;fallback=false;watchGrant()
    }else if(actual){close();return}
    actual={id:lease.id,generation:lease.generation,kind:lease.kind};grant.started()
  },options:{...options,isCurrent:current,
    onPlaybackEnd:()=>{ended=true;unsubscribe();grant.naturalEnd();options.onPlaybackEnd?.()},
    onNotDelivered:reason=>{close();options.onNotDelivered?.(reason)}}}
}

export type TutorVoicePlaybackReceipt = {playback_id:string;turn_id:string;child_id:string;session_id:string;source_version:string;generation:number;
  status:'reserved'|'started'|'finished'|'cancelled';lease_id:string|null;lease_generation:number|null;lease_kind:'local_sapi'|'managed_audio'|'local_pcm'|null}
/** No-board narration's durable metadata, always attached to the actual audio attempt. */
export function ordinaryTutorPlayback(scope:Scope,options:CompanionSpeechOptions={}) {
  type Tuple={lease_id:string;lease_generation:number;lease_kind:'local_sapi'|'managed_audio'|'local_pcm'}
  type Attempt={permit:ReturnType<typeof ordinaryPromptPlayback>;row?:TutorVoicePlaybackReceipt;tuple?:Tuple;
    startAck?:Promise<void>;startConfirmed:boolean;cancelPromise?:Promise<boolean>;retired:boolean;ended:boolean}
  let closed=false,transition=false,fallbackUsed=false,deadline=0
  let fallbackLease:Readonly<{id:string;generation:number}>|undefined
  let active:Attempt
  const create=():Attempt=>({permit:ordinaryPromptPlayback({...scope,deliveryPending:true},options,false),startConfirmed:false,retired:false,ended:false})
  active=create()
  const provenance=active.permit.grant
  const provenanceCurrent=()=>!closed&&provenance.promptCurrent()&&(options.isCurrent?.()??true)
  const current=()=>provenanceCurrent()&&(transition
    ?Date.now()<deadline&&(!grants.has(scope.sessionId)||grants.get(scope.sessionId)===active.permit.grant)
    :active.permit.grant.playbackCurrent())
  const identity=(value:TutorVoicePlaybackReceipt,status:TutorVoicePlaybackReceipt['status'],row?:TutorVoicePlaybackReceipt)=>value.status===status
    &&value.session_id===scope.sessionId&&value.child_id===scope.childId&&value.turn_id===scope.sourceTutorTurnId
    &&!!value.playback_id&&!!value.source_version&&Number.isSafeInteger(value.generation)&&value.generation>0
    &&(!row||(value.playback_id===row.playback_id&&value.source_version===row.source_version&&value.generation===row.generation))
  const tupleMatches=(value:TutorVoicePlaybackReceipt,tuple?:Tuple)=>value.lease_id===(tuple?.lease_id??null)
    &&value.lease_generation===(tuple?.lease_generation??null)&&value.lease_kind===(tuple?.lease_kind??null)
  const binding=(attempt:Attempt)=>attempt.row?{playback_id:attempt.row.playback_id,source_version:attempt.row.source_version,generation:attempt.row.generation,
    ...(attempt.startConfirmed?attempt.tuple:{})}:{}
  async function write(operation:'reserve'|'start'|'natural-end'|'cancel',body:Record<string,unknown>,upstream?:AbortSignal,until=Date.now()+5000,
    observed?:(value:TutorVoicePlaybackReceipt)=>void,allowLate=false):Promise<TutorVoicePlaybackReceipt> {
    const controller=new AbortController(),abort=()=>controller.abort(),timer=setTimeout(abort,Math.max(0,until-Date.now()))
    upstream?.addEventListener('abort',abort,{once:true});if(upstream?.aborted)abort()
    let stop=()=>{}
    const cancelled=new Promise<never>((_,reject)=>{stop=()=>reject(Error('ORDINARY_TUTOR_ACK_UNKNOWN'));if(!allowLate)controller.signal.addEventListener('abort',stop,{once:true})})
    try {
      if(controller.signal.aborted)throw Error('ORDINARY_TUTOR_ACK_UNKNOWN')
      const request=localApi.tutorVoicePlayback(scope.sessionId,operation,{...body,client_action_id:crypto.randomUUID()},controller.signal)
        .then(value=>{observed?.(value);return value})
      return allowLate?await request:await Promise.race([request,cancelled])
    }finally{clearTimeout(timer);upstream?.removeEventListener('abort',abort);controller.signal.removeEventListener('abort',stop)}
  }
  const cancelAttempt=(attempt:Attempt,signal?:AbortSignal,until=Date.now()+5000):Promise<boolean>=>{
    if(attempt.cancelPromise)return attempt.cancelPromise
    attempt.cancelPromise=(async()=>{
      try{
        if(attempt.startAck)await attempt.startAck
        if(!attempt.row)return false
        const value=await write('cancel',binding(attempt),signal,until)
        return identity(value,'cancelled',attempt.row)&&tupleMatches(value,attempt.startConfirmed?attempt.tuple:undefined)
      }catch{return false}
    })()
    return attempt.cancelPromise
  }
  const close=()=>{if(closed)return;closed=true;active.permit.grant.revoke();if(active.row)void cancelAttempt(active)}
  const watch=(attempt:Attempt)=>attempt.permit.grant.subscribeRevoked(()=>{
    if(!attempt.retired&&!attempt.permit.grant.wasAccepted()){close();if(attempt.row)void cancelAttempt(attempt)}
  })
  watch(active)
  const reserve=async(attempt:Attempt,signal:AbortSignal,until:number,previous?:TutorVoicePlaybackReceipt)=>{
    if(signal.aborted||!current())throw Error('ORDINARY_TUTOR_SOURCE_CHANGED')
    const value=await write('reserve',{turn_id:scope.sourceTutorTurnId},signal,until,returned=>{
      // A late, owned reservation is recorded even after abort, solely to close it.
      if(identity(returned,'reserved')){
        attempt.row=Object.freeze({...returned})
        if(closed||signal.aborted||active!==attempt)void cancelAttempt(attempt)
      }
    })
    if(!current()||signal.aborted||!identity(value,'reserved')||!tupleMatches(value)
      ||(previous&&(value.playback_id===previous.playback_id||value.source_version!==previous.source_version||value.generation<=previous.generation)))throw Error('ORDINARY_TUTOR_RESERVE_UNCONFIRMED')
    attempt.row=Object.freeze({...value});attempt.permit.grant.attachTutorReceipt(value.playback_id)
  }
  return {get grant(){return active.permit.grant},started:()=>{
    const attempt=active,lease=getCompanionPlaybackLease()
    if(!current()||!attempt.row||attempt.tuple||!lease||lease.sessionId!==scope.sessionId||!['local_sapi','managed','local_pcm'].includes(lease.kind)
      ||(fallbackUsed&&(lease.kind!=='local_sapi'||lease.text!==scope.text||lease.id!==fallbackLease?.id||lease.generation!==fallbackLease.generation))){close();return}
    attempt.permit.started()
    if(!attempt.permit.grant.playingCurrent()){close();return}
    attempt.tuple=Object.freeze({lease_id:lease.id,lease_generation:lease.generation,lease_kind:lease.kind==='managed'?'managed_audio':lease.kind as 'local_sapi'|'local_pcm'})
    const tuple=attempt.tuple,row=attempt.row
    attempt.startAck=write('start',{playback_id:row.playback_id,source_version:row.source_version,generation:row.generation,...tuple}).then(value=>{
      if(!identity(value,'started',row)||!tupleMatches(value,tuple))throw Error('ORDINARY_TUTOR_START_UNCONFIRMED')
      attempt.startConfirmed=true
    })
    void attempt.startAck.catch(()=>{if(!attempt.retired)close()})
  },options:{...options,isCurrent:current,
    beforePlayback:async(signal:AbortSignal)=>{
      if(!current()||!scope.sourceTutorTurnId)return false
      try{
        if(options.beforePlayback&&!await options.beforePlayback(signal))return false
        if(!current()||signal.aborted)return false
        await reserve(active,signal,Date.now()+5000);return true
      }catch{close();return false}
    },beforeLocalFallback:async(signal:AbortSignal,lease:Readonly<import('./companionVoice').CompanionPlaybackLease>)=>{
      const old=active,tuple=old.tuple,row=old.row
      if(fallbackUsed||signal.aborted||!current()||!old.permit.grant.playingCurrent()||!row||!tuple||!old.startAck||lease.kind!=='managed'
        ||tuple.lease_kind!=='managed_audio'||lease.id!==tuple.lease_id||lease.generation!==tuple.lease_generation||lease.sessionId!==scope.sessionId)return false
      fallbackUsed=true;fallbackLease=Object.freeze({id:lease.id,generation:lease.generation});transition=true;deadline=Math.min(Date.now()+5000,options.expiresAt??Infinity)
      try{
        await old.startAck
        if(signal.aborted||!current()||!old.startConfirmed)throw Error('ORDINARY_TUTOR_START_UNCONFIRMED')
        if(!await cancelAttempt(old,signal,deadline)||signal.aborted||!current())throw Error('ORDINARY_TUTOR_CANCEL_UNCONFIRMED')
        old.retired=true;old.permit.grant.revoke()
        if(!current())throw Error('ORDINARY_TUTOR_SOURCE_CHANGED')
        active=create();watch(active)
        await reserve(active,signal,deadline,row)
        if(signal.aborted||!current())throw Error('ORDINARY_TUTOR_FALLBACK_UNCONFIRMED')
        transition=false;return true
      }catch{close();return false}
    },onPlaybackEnd:()=>{
      const attempt=active,row=attempt.row,tuple=attempt.tuple
      if(!current()||attempt.ended||!attempt.startAck||!row||!tuple){close();return}
      attempt.ended=true;attempt.permit.grant.naturalEnd()
      void attempt.startAck.then(()=>{
        if(active!==attempt||!current())throw Error('ORDINARY_TUTOR_SOURCE_CHANGED')
        // Preserve the existing capture/submission window for a final ACK;
        // confirmation never renews it. The fallback transaction stays strict.
        return write('natural-end',{playback_id:row.playback_id,source_version:row.source_version,generation:row.generation,...tuple},undefined,Date.now()+5000,undefined,true)
      }).then(value=>{
        if(active!==attempt||!current()||!identity(value,'finished',row)||!tupleMatches(value,tuple)){close();return}
        attempt.permit.grant.confirmDelivery();options.onPlaybackEnd?.()
      }).catch(()=>{if(active===attempt)close()})
    },onNotDelivered:(reason:Parameters<NonNullable<CompanionSpeechOptions['onNotDelivered']>>[0])=>{close();options.onNotDelivered?.(reason)}
  }}
}

/** Exact JSON value comparison, independent of transport object-key ordering. */
export function sameResponseValue(a:unknown,b:unknown):boolean {
  if(Object.is(a,b))return true
  if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false
  const ak=Object.keys(a).sort(),bk=Object.keys(b).sort()
  return ak.length===bk.length&&ak.every((key,index)=>key===bk[index]&&sameResponseValue((a as Record<string,unknown>)[key],(b as Record<string,unknown>)[key]))
}

export function recoverOrdinaryListenReady(sessionId:string,childId:string,capture:Pick<Capture,'owner'|'track'|'epoch'>):void {
 const grant=feedbacks.get(sessionId);if(captures.get(sessionId)?.childId===childId)grant?.recoverListenReady(capture)
}
