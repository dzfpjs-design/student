import {AdaptiveVoiceActivityDetector} from './voiceActivityDetector'
import {microphoneEchoMode} from './microphoneEchoCancellation'
import {ordinaryPlayingPrompt, ordinaryPromptSourceAgent, type OrdinaryResponseGrant} from './ordinaryResponseGrant'
import {currentManagedBargePlayback, managedBargePlaybackCurrent, managedBargeNaturalReleaseConfirmed,
 originalPlaybackRecoveryClosure,
 MAX_MANAGED_PLAYBACK_SECONDS, type ManagedBargePlayback, type OriginalPlaybackClosure} from './companionVoice'

export const BARGE_CANDIDATE_MS=12000, BARGE_PREROLL_MS=160
export const MAX_BARGE_BYTES=4*96000*(BARGE_CANDIDATE_MS+BARGE_PREROLL_MS)/1000
type Capture={sessionId:string;childId:string;track:MediaStreamTrack;owner:string;leaseId:string;epoch:symbol;scopeValue:string;documentOwner:unknown}
type Options={capture:()=>Capture|null;hold:(held:boolean)=>void;failClosed:()=>void}
type Owner={capture:Capture;grant:OrdinaryResponseGrant;player:ManagedBargePlayback;rate:number}

/** Unknown-overlap quarantine only. No stop/recognizer/upload/prompt interface.
 * A player capability proves output ownership, never a human or separated echo.
 * An independent proven stop/human source is still required for a pause grant.
 */
export class SegmentedBargeEscrow {
 private owner:Owner|null=null
 private chunks:Float32Array[]=[]
 private samples=0
 private detector=new AdaptiveVoiceActivityDetector()
 private preRoll:Float32Array[]=[]
 private preSamples=0
 private started=0
 private held=false
 private failed=false
 private recovering=false
 private recoveryPlayer:OriginalPlaybackClosure|null=null
 private quietObservation=new WeakMap<Float32Array,number>()
 private expired=false
 private disposed=false
 private quietMs=0
 private quietStarted=0
 private frameAt=0
 private timer:ReturnType<typeof setTimeout>|null=null
 private outputDeadline=0
 private quietDeadline=0
 constructor(private readonly options:Options){}
 private same(a:Capture,b:Capture|null):boolean{return !!b&&a.sessionId===b.sessionId&&a.childId===b.childId&&a.track===b.track
  &&a.owner===b.owner&&a.leaseId===b.leaseId&&a.epoch===b.epoch&&a.scopeValue===b.scopeValue&&a.documentOwner===b.documentOwner}
 private captureAlive(c:Capture|null):c is Capture{return !!c&&!!c.leaseId&&c.track.readyState==='live'&&c.track.enabled===true}
 private eligible(c:Capture|null):c is Capture{return this.captureAlive(c)&&microphoneEchoMode(c.track)==='all'}
 private wipe():void{for(const c of[...this.chunks,...this.preRoll])c.fill(0);this.chunks=[];this.preRoll=[];this.samples=this.preSamples=0}
 private reset():void{this.wipe();this.quietObservation=new WeakMap();this.recovering=false;this.recoveryPlayer=null;this.owner=null;this.detector.reset();this.started=0;this.expired=false;this.outputDeadline=this.quietDeadline=0;this.quietMs=this.quietStarted=this.frameAt=0;if(this.timer!==null)clearTimeout(this.timer);this.timer=null}
 private fail():void{
  if(this.disposed||(this.failed&&!this.recovering))return
  const capture=this.options.capture(),sameOwner=!!this.owner&&this.captureAlive(capture)&&this.same(this.owner.capture,capture)
  this.failed=true;this.held=true;this.options.hold(true)
  if(sameOwner){
   // Permanently retire the OLD PCM/candidate, never the still-valid session.
   // Only existing recorder frames may passively observe the same original
   // player's genuine closure and latest original output closure, restored
   // AEC and fresh quiet. No new task,
   // timer, capture lease, source guard or response grant is minted/renewed.
   this.wipe();this.detector.reset();this.expired=true;this.recovering=true
   this.recoveryPlayer=null
   this.started=this.outputDeadline=this.quietDeadline=0
   this.quietMs=this.quietStarted=this.frameAt=0
   if(this.timer!==null)clearTimeout(this.timer);this.timer=null
  }else{this.reset();this.options.failClosed()}
 }
 private observeRecovery(frame:Float32Array,rate:number,now:number,capture:Capture|null):void{
  const owner=this.owner
  if(!owner||!this.captureAlive(capture)||!this.same(owner.capture,capture)){this.fail();return}
  const valid=rate===owner.rate&&Number.isFinite(rate)&&rate>=8000&&rate<=96000&&frame.length>0
   &&frame.length<=4096&&frame.length/rate*1000<=64&&frame.every(Number.isFinite)
  const continuous=!this.frameAt||(now>=this.frameAt&&now-this.frameAt<=500)
  this.frameAt=now
  const agent=ordinaryPromptSourceAgent(capture.sessionId,capture.childId)
  const closed=originalPlaybackRecoveryClosure(owner.player,capture.sessionId,agent??undefined)
  if(!valid||!continuous||!this.eligible(capture)||!closed){
   this.recoveryPlayer=null
   this.quietMs=this.quietStarted=0;this.detector.reset();return
  }
  // A new original player is an output fact, not permission for old PCM.
  // Each successor's actual closure starts its own fresh quiet observation.
  if(this.recoveryPlayer!==closed){this.recoveryPlayer=closed;this.quietMs=this.quietStarted=0;this.detector.reset()}
  let energy=0;for(const n of frame)energy+=n*n
  const ms=frame.length/rate*1000,activity=this.detector.observe(Math.sqrt(energy/frame.length),ms)
  if(activity.voiced){this.quietMs=this.quietStarted=0;return}
  this.quietObservation.set(frame,rate)
  if(!this.quietStarted)this.quietStarted=now
  this.quietMs+=ms
  if(this.quietMs>=850&&now-this.quietStarted>=850){
   // All old audio remains erased. Release only local input quarantine;
   // the next NEW sentence still uses every original recorder/ASR guard.
   this.reset();this.failed=false;this.held=false;this.options.hold(false)
  }
 }
 private expireCandidate():void{
  if(this.expired||this.failed||this.disposed)return
  this.expired=true;this.wipe()
  if(this.timer!==null)clearTimeout(this.timer)
  // Audio is gone. Do not revoke the capture (and indirectly stop its still-
  // owned TTS). Metadata wait is bounded by the ORIGINAL player 300s+close cap.
  this.timer=setTimeout(()=>this.fail(),Math.max(0,(this.quietDeadline||this.outputDeadline)-performance.now()))
 }
 private buffer(frame:Float32Array,rate:number):void{
  if((this.samples+frame.length)*4>MAX_BARGE_BYTES||this.samples+frame.length>Math.ceil(rate*12.16)){this.expireCandidate();return}
  this.chunks.push(frame.slice());this.samples+=frame.length
 }
 private remember(frame:Float32Array,rate:number):void{
  const limit=Math.floor(rate*.16),copy=frame.slice(Math.max(0,frame.length-limit));this.preRoll.push(copy);this.preSamples+=copy.length
  while(this.preSamples>limit){const first=this.preRoll[0],drop=Math.min(first.length,this.preSamples-limit)
   if(drop===first.length)this.preRoll.shift();else this.preRoll[0]=first.slice(drop)
   first.fill(0);this.preSamples-=drop}
 }
 /** Observation only: the identical validated quiet processor frame may reach
  * original quiet/cue observers, never ordinary PCM buffering or ASR while held.
  * Weak keys retain no PCM; each processFrame invalidates previous-frame facts.
  */
 mayObserveRecoveryQuiet(frame:Float32Array,rate:number):boolean{return !this.disposed&&this.held&&this.quietObservation.get(frame)===rate}
 processFrame(frame:Float32Array,rate:number):void{
  this.quietObservation=new WeakMap()
  if(this.disposed)return
  const now=performance.now(),capture=this.options.capture()
  if(this.recovering){this.observeRecovery(frame,rate,now,capture);return}
  if(this.failed)return
  if(!Number.isFinite(rate)||rate<8000||rate>96000||!frame.length||frame.length>4096||frame.length/rate*1000>64||!frame.every(Number.isFinite)){
   if(this.held)this.fail();else this.reset();return
  }
  if(this.frameAt&&(now<this.frameAt||now-this.frameAt>500)){if(this.held){this.fail();return}this.reset()}
  if(this.owner&&(!this.captureAlive(capture)||!this.same(this.owner.capture,capture)||!this.owner.grant.promptCurrent()||this.owner.rate!==rate)){
   if(this.held)this.fail();else this.reset();return
  }
  if(!this.owner){
   if(!this.eligible(capture)){this.reset();return}
   const grant=ordinaryPlayingPrompt(capture.sessionId,capture.childId),agent=grant?.binding.source_agent_turn_id
   const player=agent?currentManagedBargePlayback(capture.sessionId,agent):null
   if(!grant||!player){this.reset();return}
   this.owner={capture:{...capture},grant,player,rate}
  }
  if(this.owner&&!this.eligible(capture)){
   if(!this.held){this.reset();return}
   // AEC-only loss wipes the entire audio immediately, without extending the
   // original12s cap or revoking output ownership. Metadata remains bounded by
   // the original output/post-close deadlines. Unknown AEC cannot count quiet.
   this.expireCandidate();this.quietMs=this.quietStarted=0
  }
  this.frameAt=now
  let energy=0;for(const n of frame)energy+=n*n
  const ms=frame.length/rate*1000,activity=this.detector.observe(Math.sqrt(energy/frame.length),ms)
  const active=managedBargePlaybackCurrent(this.owner.player)
  if(!this.held){
   if(!active){this.reset();return}
   if(!activity.voiced){this.remember(frame,rate);return}
   this.held=true;this.started=now;this.chunks=this.preRoll;this.samples=this.preSamples;this.preRoll=[];this.preSamples=0
   this.outputDeadline=now+MAX_MANAGED_PLAYBACK_SECONDS*1000+1500
   this.options.hold(true)
   // One fixed deadline. Quiet, new frames, and owner recreation never renew it.
   this.timer=setTimeout(()=>this.expireCandidate(),BARGE_CANDIDATE_MS)
  }
  if(now-this.started>=BARGE_CANDIDATE_MS)this.expireCandidate()
  if(!this.expired)this.buffer(frame,rate)
  if(this.failed)return
  if(active){this.quietMs=this.quietStarted=0;return}
  if(!managedBargeNaturalReleaseConfirmed(this.owner.player)){this.fail();return}
  if(!this.quietDeadline){
   this.quietDeadline=now+BARGE_CANDIDATE_MS
   // Preserve the original audio-wipe deadline if audio still exists. Its
   // expiry then arms this independent, fixed metadata quiet deadline.
   if(this.expired){if(this.timer!==null)clearTimeout(this.timer);this.timer=setTimeout(()=>this.fail(),BARGE_CANDIDATE_MS)}
  }
  if(now>=this.quietDeadline){this.fail();return}
  if(!this.eligible(capture)||activity.voiced){this.quietMs=this.quietStarted=0;return}
  this.quietObservation.set(frame,rate)
  if(!this.quietStarted)this.quietStarted=now
  this.quietMs+=ms
  if(this.quietMs>=850&&now-this.quietStarted>=850){
   // Drop the ENTIRE unknown sentence, including its prefix. A suffix cannot
   // become a new answer. Only a later fresh utterance uses the original chain.
   this.reset();this.held=false;this.options.hold(false)
  }
 }
 dispose():void{if(this.disposed)return;this.disposed=true;this.reset();this.held=false;this.options.hold(false)}
}
